from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import FakeManager, login

from opencode_web.app import create_app
from opencode_web.artifacts import ArtifactError, ConsultationArtifacts
from opencode_web.database import Database

PASSWORD = "long-enough-password"


def _chat_and_job(
    db: Database,
    user: dict,
    folder: Path,
    *,
    session_id: str = "session-1",
    content: str = "Question",
    status: str = "running",
):
    chat = db.create_chat(
        user["id"], session_id, "Title", str(folder), consultation=True
    )
    job = db.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content=content,
        agent="build",
        provider_id="test",
        model_id="coder",
        model_variant=None,
    )
    if status != "queued":
        db.set_job_status(job["id"], status)
    return chat, db.get_job(job["id"])


@pytest.fixture
def artifact_stack(settings, workspace):
    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    folder = workspace / "worker"
    folder.mkdir()
    user = db.create_user("worker", PASSWORD, "user", str(folder))
    artifacts = ConsultationArtifacts(settings, db)
    artifacts.initialize()
    chat, job = _chat_and_job(db, user, folder)
    paths = artifacts.prepare_job(chat["id"], job["id"])
    yield SimpleNamespace(
        db=db,
        artifacts=artifacts,
        user=user,
        folder=folder,
        chat=chat,
        job=job,
        paths=paths,
    )
    db.close()


def test_text_artifact_is_pending_until_answer_completes(artifact_stack):
    t = artifact_stack
    artifact = t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="hello", name="report.txt"
    )
    assert artifact["status"] == "pending"
    assert t.db.consultation_artifacts_for_chat(t.chat["id"]) == []
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.download_path(t.chat["id"], artifact["id"])
    assert caught.value.code == "not_ready"

    assert t.db.complete_consultation_run(t.job["id"], "Final answer")
    ready = t.db.consultation_artifact(t.chat["id"], artifact["id"])
    assert ready["status"] == "ready"
    path, metadata = t.artifacts.download_path(t.chat["id"], artifact["id"])
    assert path.read_text(encoding="utf-8") == "hello"
    assert metadata["original_name"] == "report.txt"


def test_file_artifact_uses_opaque_storage_and_deduplicates(artifact_stack):
    t = artifact_stack
    source = t.paths.outbox / "result.md"
    source.write_text("same content", encoding="utf-8")
    first = t.artifacts.publish_file_for_job(
        t.chat["id"], t.job["id"], path=source
    )
    second = t.artifacts.publish_file_for_job(
        t.chat["id"], t.job["id"], path=source, name="copy.md"
    )
    assert first["id"] == second["id"]
    assert first["storage_key"] != str(source)
    assert first["status"] == "pending"
    rows = t.db.consultation_artifacts_for_chat(
        t.chat["id"], statuses=("pending", "ready")
    )
    assert [row["id"] for row in rows] == [first["id"]]
    stored = t.artifacts._storage_path(first["storage_key"])
    assert stored.read_text(encoding="utf-8") == "same content"
    assert stored.stat().st_mode & 0o777 == 0o600


def test_active_content_is_never_served_with_active_mime(artifact_stack):
    t = artifact_stack
    source = t.paths.outbox / "page.html"
    source.write_text("<script>alert(1)</script>", encoding="utf-8")
    artifact = t.artifacts.publish_file_for_job(
        t.chat["id"], t.job["id"], path=source
    )
    assert artifact["content_type"] == "application/octet-stream"
    explicit = t.artifacts.publish_text_for_job(
        t.chat["id"],
        t.job["id"],
        text="<svg/>",
        name="image.svg",
        content_type="image/svg+xml",
    )
    assert explicit["content_type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "sub/file.txt", "..", "", "bad\x00name"],
)
def test_invalid_names_are_rejected(artifact_stack, name):
    t = artifact_stack
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="content", name=name
        )
    assert caught.value.code == "invalid_name"


def test_paths_outside_the_job_outbox_are_rejected(artifact_stack, tmp_path):
    t = artifact_stack
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=outside
        )
    assert caught.value.code == "outside_outbox"

    nested = t.paths.outbox / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("content", encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=nested / "file.txt"
        )
    assert caught.value.code == "outside_outbox"

    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path="relative.txt"
        )
    assert caught.value.code == "invalid_path"


def test_symlink_hardlink_and_directories_are_rejected(artifact_stack, tmp_path):
    t = artifact_stack
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    link = t.paths.outbox / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=link
        )
    assert caught.value.code == "unsafe_path"

    directory = t.paths.outbox / "directory"
    directory.mkdir()
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=directory
        )
    assert caught.value.code == "not_regular"

    hardlink = t.paths.outbox / "hardlink.txt"
    os.link(outside, hardlink)
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=hardlink
        )
    assert caught.value.code == "hardlink"


def test_size_file_and_total_limits(artifact_stack):
    t = artifact_stack
    source = t.paths.outbox / "large.bin"
    source.write_bytes(b"12345")
    t.artifacts.max_bytes = 4
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_file_for_job(
            t.chat["id"], t.job["id"], path=source
        )
    assert caught.value.code == "too_large"

    t.artifacts.max_bytes = 100
    t.artifacts.max_text_bytes = 4
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="12345", name="a.txt"
        )
    assert caught.value.code == "too_large"
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="", name="a.txt"
        )
    assert caught.value.code == "empty_text"

    t.artifacts.max_text_bytes = 100
    t.artifacts.max_files = 1
    t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="one", name="one.txt"
    )
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="two", name="two.txt"
        )
    assert caught.value.code == "artifact_file_limit"

    t.artifacts.max_files = 20
    t.artifacts.max_total_bytes = 8
    t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="12345", name="three.txt"
    )
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="x", name="four.txt"
        )
    assert caught.value.code == "artifact_total_limit"


def test_staging_components_must_not_be_symlinks(artifact_stack, tmp_path):
    t = artifact_stack
    chat_link = t.artifacts.staging_root / "chat-link"
    chat_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.prepare_job("chat-link", "job")
    assert caught.value.code == "unsafe_staging"


def test_outbox_fallback_publishes_only_safe_top_level_files(artifact_stack):
    t = artifact_stack
    (t.paths.outbox / "report.txt").write_text("report", encoding="utf-8")
    nested = t.paths.outbox / "nested"
    nested.mkdir()
    (nested / "ignored.txt").write_text("ignored", encoding="utf-8")
    (t.paths.outbox / ".hidden").write_text("hidden", encoding="utf-8")
    (t.paths.outbox / "link").symlink_to(t.paths.outbox / "report.txt")

    published = t.artifacts.publish_outbox(t.chat["id"], t.job["id"])
    assert [item["original_name"] for item in published] == ["report.txt"]
    assert published[0]["status"] == "pending"


def test_cleanup_removes_pending_but_keeps_ready_artifacts(artifact_stack):
    t = artifact_stack
    pending = t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="pending", name="pending.txt"
    )
    pending_path = t.artifacts._storage_path(pending["storage_key"])
    t.artifacts.cleanup_job(t.job["id"])
    assert t.db.consultation_artifact(t.chat["id"], pending["id"]) is None
    assert not pending_path.exists()

    ready = t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="ready", name="ready.txt"
    )
    assert t.db.complete_consultation_run(t.job["id"], "answer")
    ready_path = t.artifacts._storage_path(ready["storage_key"])
    t.artifacts.cleanup_job(t.job["id"])
    assert t.db.consultation_artifact(t.chat["id"], ready["id"])["status"] == "ready"
    assert ready_path.exists()


def test_missing_file_marks_artifact_missing(artifact_stack):
    t = artifact_stack
    artifact = t.artifacts.publish_text_for_job(
        t.chat["id"], t.job["id"], text="temporary", name="gone.txt"
    )
    assert t.db.complete_consultation_run(t.job["id"], "answer")
    path, _ = t.artifacts.download_path(t.chat["id"], artifact["id"])
    path.unlink()
    with pytest.raises(ArtifactError) as caught:
        t.artifacts.download_path(t.chat["id"], artifact["id"])
    assert caught.value.code == "missing"
    assert t.db.consultation_artifact(t.chat["id"], artifact["id"])["status"] == "missing"


@pytest.mark.asyncio
async def test_session_resolution_accepts_descendants_but_not_foreign_workspaces(
    artifact_stack, workspace
):
    t = artifact_stack
    manager = FakeManager()
    manager.client.session_records["session-1"] = {
        "id": "session-1",
        "directory": str(t.folder),
    }
    manager.client.session_records["session-child"] = {
        "id": "session-child",
        "parentID": "session-1",
        "directory": str(t.folder),
    }
    t.artifacts.manager = manager

    direct = await t.artifacts.publish_from_session(
        "session-1", text="direct", name="direct.txt"
    )
    assert direct["status"] == "pending"
    child = await t.artifacts.publish_from_session(
        "session-child", text="child", name="child.txt"
    )
    assert child["status"] == "pending"

    manager.client.session_records["session-child"]["directory"] = str(
        workspace / "foreign"
    )
    with pytest.raises(ArtifactError) as caught:
        await t.artifacts.publish_from_session(
            "session-child", text="foreign", name="foreign.txt"
        )
    assert caught.value.code == "session_not_found"


@pytest.mark.asyncio
async def test_unknown_session_and_inactive_job_are_rejected(artifact_stack):
    t = artifact_stack
    with pytest.raises(ArtifactError) as caught:
        await t.artifacts.publish_from_session(
            "unknown-session", text="content", name="x.txt"
        )
    assert caught.value.code == "session_not_found"

    t.db.set_job_status(t.job["id"], "completed")
    with pytest.raises(ArtifactError) as caught:
        await t.artifacts.publish_text_for_job(
            t.chat["id"], t.job["id"], text="content", name="x.txt"
        )
    assert caught.value.code == "inactive_job"


@pytest.fixture
def artifact_api(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        app.state.manager = FakeManager()
        db = app.state.database
        worker_folder = workspace / "worker"
        worker_folder.mkdir()
        stranger_folder = workspace / "stranger"
        stranger_folder.mkdir()
        worker = db.create_user("worker", PASSWORD, "user", str(worker_folder))
        db.create_user("stranger", PASSWORD, "user", str(stranger_folder))
        chat, job = _chat_and_job(db, worker, worker_folder)
        paths = app.state.artifacts.prepare_job(chat["id"], job["id"])
        csrf = login(client, "worker", PASSWORD)
        yield SimpleNamespace(
            app=app,
            client=client,
            db=db,
            worker=worker,
            folder=worker_folder,
            chat=chat,
            job=job,
            paths=paths,
            csrf=csrf,
            password=PASSWORD,
        )


def test_internal_endpoint_publishes_and_is_token_protected(artifact_api):
    t = artifact_api
    token = t.app.state.artifacts.token
    headers = {"Authorization": f"Bearer {token}"}

    published = t.client.post(
        "/api/internal/consultation-artifacts",
        json={
            "session_id": "session-1",
            "text": "hello",
            "name": "note.txt",
            "description": "Summary",
        },
        headers=headers,
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["chat_id"] == t.chat["id"]
    assert body["job_id"] == t.job["id"]
    assert body["status"] == "pending"
    assert body["download_url"].startswith("/api/consultations/")

    assert t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-1", "text": "x", "name": "x.txt"},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401
    assert t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-1", "text": "x", "name": "x.txt"},
    ).status_code == 401
    assert t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-1", "text": "x", "path": "/tmp/x"},
        headers=headers,
    ).status_code == 422
    assert t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "missing", "text": "x", "name": "x.txt"},
        headers=headers,
    ).status_code == 404

    inactive_chat, _ = _chat_and_job(
        t.db,
        t.worker,
        t.folder,
        session_id="session-inactive",
        content="Other",
        status="queued",
    )
    assert t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-inactive", "text": "x", "name": "x.txt"},
        headers=headers,
    ).status_code == 409
    assert inactive_chat["id"] != t.chat["id"]


def test_internal_endpoint_publishes_outbox_file(artifact_api):
    t = artifact_api
    source = t.paths.outbox / "result.txt"
    source.write_text("file content", encoding="utf-8")
    response = t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-1", "path": str(source), "name": "result.txt"},
        headers={"Authorization": f"Bearer {t.app.state.artifacts.token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


def test_detail_and_download_are_owner_scoped_and_ready_only(artifact_api):
    t = artifact_api
    token = t.app.state.artifacts.token
    artifact_id = t.client.post(
        "/api/internal/consultation-artifacts",
        json={"session_id": "session-1", "text": "hello", "name": "report.txt"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["id"]

    detail = t.client.get(f"/api/consultations/{t.chat['id']}")
    assert detail.status_code == 200
    assert all(
        "artifacts" not in message
        for message in detail.json()["messages"]
        if message["role"] == "assistant"
    )
    pending_download = t.client.get(
        f"/api/consultations/{t.chat['id']}/artifacts/{artifact_id}"
    )
    assert pending_download.status_code == 410

    assert t.db.complete_consultation_run(t.job["id"], "Final answer")
    detail = t.client.get(f"/api/consultations/{t.chat['id']}")
    assistant = [
        message for message in detail.json()["messages"] if message["role"] == "assistant"
    ][0]
    assert [item["id"] for item in assistant["artifacts"]] == [artifact_id]
    assert assistant["artifacts"][0]["name"] == "report.txt"

    download = t.client.get(
        f"/api/consultations/{t.chat['id']}/artifacts/{artifact_id}"
    )
    assert download.status_code == 200
    assert download.content == b"hello"
    assert download.headers["content-type"].startswith("text/plain")
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"

    login(t.client, "stranger", t.password)
    assert t.client.get(
        f"/api/consultations/{t.chat['id']}/artifacts/{artifact_id}"
    ).status_code == 404

    admin_csrf = login(t.client, "admin", "correct-horse-battery-staple")
    admin_download = t.client.get(
        f"/api/consultations/{t.chat['id']}/artifacts/{artifact_id}",
        headers={"x-csrf-token": admin_csrf},
    )
    assert admin_download.status_code == 200
    assert any(
        row["action"] == "consultation.artifact.download"
        for row in t.db.list_audit()
    )
