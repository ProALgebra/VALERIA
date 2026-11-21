from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from test_api import FakeManager, login

from opencode_web.app import create_app
from opencode_web.artifacts import ConsultationArtifacts
from opencode_web.attachments import AttachmentError, ConsultationAttachments
from opencode_web.database import Database


def upload(name: str, data: bytes, content_type: str = "text/plain") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers={"content-type": content_type},
    )


@pytest.fixture
def attachment_case(settings, workspace):
    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    folder = workspace / "worker"
    folder.mkdir()
    user = db.create_user("worker", "long-enough-password", "user", str(folder))
    artifacts = ConsultationArtifacts(settings, db)
    artifacts.initialize()
    attachments = ConsultationAttachments(settings, db, artifacts)
    attachments.initialize()
    chat = db.create_chat(
        user["id"], "consultation-session", "Consultation", str(folder), consultation=True
    )
    job = db.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="Question",
        agent="build",
        provider_id="test",
        model_id="coder",
        model_variant=None,
    )
    staging = artifacts.prepare_job(chat["id"], job["id"])
    yield SimpleNamespace(
        db=db,
        user=user,
        artifacts=artifacts,
        attachments=attachments,
        chat=chat,
        job=job,
        staging=staging,
    )
    db.close()


async def test_spool_stores_file_and_rejects_empty_and_oversized(attachment_case):
    t = attachment_case
    spooled = await t.attachments.spool([upload("notes.txt", b"hello")])
    assert len(spooled) == 1
    item = spooled[0]
    assert item.original_name == "notes.txt"
    assert item.content_type == "text/plain"
    assert item.size_bytes == 5
    assert item.temp_path.is_file()
    assert item.temp_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(AttachmentError) as empty:
        await t.attachments.spool([upload("empty.txt", b"")])
    assert empty.value.code == "empty_file"

    t.attachments.max_bytes = 4
    with pytest.raises(AttachmentError) as large:
        await t.attachments.spool([upload("large.txt", b"12345")])
    assert large.value.code == "too_large"

    t.attachments.max_files = 1
    with pytest.raises(AttachmentError) as count:
        await t.attachments.spool([upload("a.txt", b"a"), upload("b.txt", b"b")])
    assert count.value.code == "file_limit"

    t.attachments.max_files = 10
    t.attachments.max_bytes = 20
    t.attachments.max_total_bytes = 5
    with pytest.raises(AttachmentError) as total:
        await t.attachments.spool([upload("a.txt", b"123"), upload("b.txt", b"456")])
    assert total.value.code == "total_limit"


async def test_attach_job_persists_file_and_copies_to_inbox(attachment_case):
    t = attachment_case
    spooled = await t.attachments.spool([upload("notes.txt", b"hello")])
    rows = t.attachments.attach_job(t.chat["id"], t.job["id"], spooled)
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_id"] == t.chat["id"]
    assert row["job_id"] == t.job["id"]
    assert row["original_name"] == "notes.txt"
    assert row["sha256"]
    assert not spooled[0].temp_path.exists()

    durable = t.attachments._storage_path(row["storage_key"])
    assert durable.read_bytes() == b"hello"
    assert durable.stat().st_mode & 0o777 == 0o600

    inbox = t.staging.inbox / f"{row['id'][:12]}-notes.txt"
    assert inbox.read_bytes() == b"hello"
    staged = t.attachments.ensure_inbox(t.job["id"])
    assert staged == [
        {
            "name": "notes.txt",
            "path": str(inbox),
            "content_type": "text/plain",
            "size_bytes": 5,
        }
    ]

    path, metadata = t.attachments.download_path(t.chat["id"], row["id"])
    assert path.read_bytes() == b"hello"
    assert metadata["id"] == row["id"]
    public = t.attachments.public_attachment(row) if hasattr(
        t.attachments, "public_attachment"
    ) else None
    if public is not None:
        assert public["download_url"].endswith(f"/attachments/{row['id']}")


async def test_attach_job_deduplicates_same_content(attachment_case):
    t = attachment_case
    first = t.attachments.attach_job(
        t.chat["id"], t.job["id"], await t.attachments.spool([upload("a.txt", b"same")])
    )
    second = t.attachments.attach_job(
        t.chat["id"], t.job["id"], await t.attachments.spool([upload("b.txt", b"same")])
    )
    assert second[0]["id"] == first[0]["id"]
    rows = t.db.consultation_attachments_for_chat(t.chat["id"])
    assert len(rows) == 1
    assert (t.staging.inbox / f"{rows[0]['id'][:12]}-a.txt").is_file()


async def test_spool_sanitizes_name_and_blocks_active_content(attachment_case):
    t = attachment_case
    with pytest.raises(AttachmentError) as traversal:
        await t.attachments.spool([upload("../secret.txt", b"x")])
    assert traversal.value.code == "invalid_name"

    with pytest.raises(AttachmentError) as dotdot:
        await t.attachments.spool([upload("..", b"x")])
    assert dotdot.value.code == "invalid_name"

    spooled = await t.attachments.spool([upload("page.html", b"<script>", "text/html")])
    assert spooled[0].original_name == "page.html"
    assert spooled[0].content_type == "application/octet-stream"


async def test_missing_or_symlinked_durable_file_is_rejected(attachment_case):
    t = attachment_case
    rows = t.attachments.attach_job(
        t.chat["id"], t.job["id"], await t.attachments.spool([upload("a.txt", b"data")])
    )
    durable = t.attachments._storage_path(rows[0]["storage_key"])
    durable.unlink()
    with pytest.raises(AttachmentError) as missing:
        t.attachments.download_path(t.chat["id"], rows[0]["id"])
    assert missing.value.code == "missing"
    with pytest.raises(AttachmentError) as inbox_missing:
        t.attachments.ensure_inbox(t.job["id"])
    assert inbox_missing.value.code == "missing"

    rows[0] = t.db.consultation_attachment(t.chat["id"], rows[0]["id"])
    durable.write_bytes(b"data")
    outside = durable.parent.parent / "outside.txt"
    outside.write_bytes(b"outside")
    durable.unlink()
    durable.symlink_to(outside)
    with pytest.raises(AttachmentError) as linked:
        t.attachments.download_path(t.chat["id"], rows[0]["id"])
    assert linked.value.code == "missing"


async def test_attach_rejects_job_that_is_no_longer_active(attachment_case):
    t = attachment_case
    assert t.db.complete_consultation_run(t.job["id"], "answer")
    spooled = await t.attachments.spool([upload("late.txt", b"late")])
    with pytest.raises(ValueError) as inactive:
        t.attachments.attach_job(t.chat["id"], t.job["id"], spooled)
    assert str(inactive.value) == "inactive_consultation_job"


@pytest.fixture
def attachment_api(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        manager = FakeManager()
        app.state.manager = manager
        db = app.state.database
        users = {}
        for name in ("worker", "stranger"):
            folder = workspace / name
            folder.mkdir()
            users[name] = db.create_user(
                name, "long-enough-password", "user", str(folder)
            )
        admin_headers = {
            "x-csrf-token": login(client, "admin", "correct-horse-battery-staple")
        }
        assert (
            client.put(
                "/api/admin/settings/consultations",
                headers=admin_headers,
                json={"model": "test/coder"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": login(client, "worker", "long-enough-password")}
        yield SimpleNamespace(
            app=app,
            client=client,
            db=db,
            users=users,
            headers=headers,
            admin_headers=admin_headers,
        )


def test_multipart_create_returns_attachment_metadata_and_download(attachment_api):
    t = attachment_api
    response = t.client.post(
        "/api/consultations",
        headers=t.headers,
        data={"message": "Изучи вложения"},
        files=[
            ("files", ("notes.txt", b"hello", "text/plain")),
            ("files", ("data.csv", b"a,b\n1,2\n", "text/csv")),
        ],
    )
    assert response.status_code == 201, response.text
    detail = response.json()
    user_message = next(item for item in detail["messages"] if item["role"] == "user")
    assert user_message["text"] == "Изучи вложения"
    assert len(user_message["attachments"]) == 2
    names = {item["name"] for item in user_message["attachments"]}
    assert names == {"notes.txt", "data.csv"}

    link = user_message["attachments"][0]["download_url"]
    download = t.client.get(link)
    assert download.status_code == 200
    assert download.content in {b"hello", b"a,b\n1,2\n"}
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"

    login(t.client, "stranger", "long-enough-password")
    assert t.client.get(link).status_code == 404

    login(t.client, "admin", "correct-horse-battery-staple")
    assert t.client.get(link).status_code == 200
    assert any(
        item["action"] == "consultation.attachment.download"
        for item in t.db.list_audit()
    )


def test_multipart_followup_and_json_compatibility(attachment_api):
    t = attachment_api
    first = t.client.post(
        "/api/consultations",
        headers=t.headers,
        json={"message": "Обычный вопрос"},
    )
    assert first.status_code == 201
    chat_id = first.json()["id"]
    assert first.json()["messages"][0]["attachments"] == []

    follow = t.client.post(
        f"/api/consultations/{chat_id}/messages",
        headers=t.headers,
        data={"message": "Изучи файл"},
        files=[("files", ("report.txt", b"report", "text/plain"))],
    )
    assert follow.status_code == 202, follow.text
    assert set(follow.json()) == {
        "id", "chat_id", "status", "created_at", "finished_at"
    }
    detail = t.client.get(f"/api/consultations/{chat_id}").json()
    second_user = [item for item in detail["messages"] if item["role"] == "user"][1]
    assert second_user["attachments"][0]["name"] == "report.txt"

    plain = t.client.post(
        f"/api/consultations/{chat_id}/messages",
        headers=t.headers,
        json={"message": "Без файлов"},
    )
    assert plain.status_code == 202
    detail = t.client.get(f"/api/consultations/{chat_id}").json()
    third_user = [item for item in detail["messages"] if item["role"] == "user"][2]
    assert third_user["attachments"] == []


def test_multipart_validation_and_limits(attachment_api):
    t = attachment_api
    missing = t.client.post(
        "/api/consultations", headers=t.headers, files=[("files", ("a.txt", b"a"))]
    )
    assert missing.status_code == 422
    extra = t.client.post(
        "/api/consultations",
        headers=t.headers,
        data={"message": "hi", "extra": "no"},
        files=[("files", ("a.txt", b"a"))],
    )
    assert extra.status_code == 422
    wrong_field = t.client.post(
        "/api/consultations",
        headers=t.headers,
        data={"message": "hi"},
        files=[("other", ("a.txt", b"a"))],
    )
    assert wrong_field.status_code == 422
    empty = t.client.post(
        "/api/consultations",
        headers=t.headers,
        data={"message": "hi"},
        files=[("files", ("empty.txt", b""))],
    )
    assert empty.status_code == 422

    t.app.state.attachments.max_bytes = 3
    large = t.client.post(
        "/api/consultations",
        headers=t.headers,
        data={"message": "hi"},
        files=[("files", ("large.txt", b"1234"))],
    )
    assert large.status_code == 413


def test_upload_is_rejected_while_waiting_for_question(attachment_api):
    t = attachment_api
    created = t.client.post(
        "/api/consultations", headers=t.headers, json={"message": "Вопрос"}
    )
    chat = created.json()
    job = t.db.consultation_jobs(chat["id"])[0]
    t.db.set_job_status(job["id"], "waiting_question")
    response = t.client.post(
        f"/api/consultations/{chat['id']}/messages",
        headers=t.headers,
        data={"message": "С файлом"},
        files=[("files", ("a.txt", b"a"))],
    )
    assert response.status_code == 409
    assert t.db.consultation_attachments_for_chat(chat["id"]) == []


def test_upload_idempotency_includes_file_fingerprint(attachment_api):
    t = attachment_api
    key = "upload-key-1"
    payload = {"message": "Изучи"}
    files = [("files", ("same.txt", b"same", "text/plain"))]
    first = t.client.post(
        "/api/consultations",
        headers={**t.headers, "idempotency-key": key},
        data=payload,
        files=files,
    )
    assert first.status_code == 201, first.text
    second = t.client.post(
        "/api/consultations",
        headers={**t.headers, "idempotency-key": key},
        data=payload,
        files=[("files", ("same.txt", b"same", "text/plain"))],
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert len(t.db.list_chats(t.users["worker"]["id"], consultation=True)) == 1

    changed = t.client.post(
        "/api/consultations",
        headers={**t.headers, "idempotency-key": key},
        data=payload,
        files=[("files", ("same.txt", b"different", "text/plain"))],
    )
    assert changed.status_code == 409
