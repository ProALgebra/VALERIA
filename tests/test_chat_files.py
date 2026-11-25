from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import FakeManager, login

from opencode_web.app import create_app

PASSWORD = "long-enough-password"


@pytest.fixture
def dialog_api(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        manager = FakeManager()
        app.state.manager = manager
        db = app.state.database
        users = {}
        for name in ("worker", "stranger"):
            folder = workspace / name
            folder.mkdir()
            users[name] = db.create_user(name, PASSWORD, "user", str(folder))
        admin_headers = {
            "x-csrf-token": login(client, "admin", "correct-horse-battery-staple")
        }
        headers = {"x-csrf-token": login(client, "worker", PASSWORD)}
        yield SimpleNamespace(
            app=app,
            client=client,
            db=db,
            users=users,
            headers=headers,
            admin_headers=admin_headers,
            manager=manager,
        )


def test_dialog_multipart_create_lists_and_downloads_files(dialog_api):
    t = dialog_api
    response = t.client.post(
        "/api/chats",
        headers=t.headers,
        data={"message": "Изучи вложения", "agent": "build", "model": "test/coder"},
        files=[
            ("files", ("notes.txt", b"hello", "text/plain")),
            ("files", ("data.csv", b"a,b\n1,2\n", "text/csv")),
        ],
    )
    assert response.status_code == 201, response.text
    created = response.json()
    chat_id = created["chat"]["id"]
    job_id = created["job"]["id"]

    messages = t.client.get(f"/api/chats/{chat_id}/messages")
    assert messages.status_code == 200, messages.text
    user_message = next(item for item in messages.json() if item["info"]["role"] == "user")
    assert user_message["info"]["id"] == created["job"]["message_id"]
    assert {item["name"] for item in user_message["attachments"]} == {
        "notes.txt",
        "data.csv",
    }
    attachment = user_message["attachments"][0]
    assert attachment["download_url"].startswith(f"/api/chats/{chat_id}/attachments/")

    download = t.client.get(attachment["download_url"])
    assert download.status_code == 200
    assert download.content in {b"hello", b"a,b\n1,2\n"}
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"

    stored = t.db.consultation_attachments_for_chat(chat_id, job_id=job_id)
    assert [row["job_id"] for row in stored] == [job_id, job_id]

    login(t.client, "stranger", PASSWORD)
    assert t.client.get(attachment["download_url"]).status_code == 404

    login(t.client, "admin", "correct-horse-battery-staple")
    assert t.client.get(attachment["download_url"]).status_code == 200
    assert any(
        item["action"] == "chat.attachment.download" for item in t.db.list_audit()
    )


def test_dialog_json_compatibility_and_multipart_followup(dialog_api):
    t = dialog_api
    created = t.client.post(
        "/api/chats",
        headers=t.headers,
        json={"message": "Обычный вопрос", "agent": "build", "model": "test/coder"},
    )
    assert created.status_code == 201, created.text
    chat_id = created.json()["chat"]["id"]
    assert created.json()["job"]["status"] == "queued"

    follow = t.client.post(
        f"/api/chats/{chat_id}/messages",
        headers=t.headers,
        data={"message": "Изучи файл", "agent": "build", "model": "test/coder"},
        files=[("files", ("report.txt", b"report", "text/plain"))],
    )
    assert follow.status_code == 202, follow.text
    assert follow.json()["status"] == "queued"

    messages = t.client.get(f"/api/chats/{chat_id}/messages").json()
    user_messages = [item for item in messages if item["info"]["role"] == "user"]
    assert user_messages[-1]["attachments"][0]["name"] == "report.txt"

    plain = t.client.post(
        f"/api/chats/{chat_id}/messages",
        headers=t.headers,
        json={"message": "Без файлов", "agent": "build", "model": "test/coder"},
    )
    assert plain.status_code == 202
    messages = t.client.get(f"/api/chats/{chat_id}/messages").json()
    assert "attachments" not in next(
        item for item in messages if item["info"]["id"] == plain.json()["message_id"]
    )


def test_dialog_multipart_validation(dialog_api):
    t = dialog_api
    missing = t.client.post(
        "/api/chats", headers=t.headers, files=[("files", ("a.txt", b"a"))]
    )
    assert missing.status_code == 422
    extra = t.client.post(
        "/api/chats",
        headers=t.headers,
        data={"message": "x", "unknown": "y"},
        files=[("files", ("a.txt", b"a"))],
    )
    assert extra.status_code == 422
    no_message = t.client.post(
        "/api/chats",
        headers=t.headers,
        files=[("files", ("a.txt", b"a"))],
    )
    assert no_message.status_code == 422


def test_dialog_artifact_pending_until_completion_and_download(dialog_api):
    t = dialog_api
    worker = t.users["worker"]
    folder = worker["workspace_path"]
    chat = t.db.create_chat(worker["id"], "chat-session", "Dialog", folder)
    job = t.db.create_job(
        chat=chat,
        effective_user_id=worker["id"],
        actor_user_id=worker["id"],
        content="Сделай отчёт",
        agent="build",
        provider_id="test",
        model_id="coder",
        model_variant=None,
    )
    t.db.set_job_status(job["id"], "running")
    t.app.state.artifacts.prepare_job(chat["id"], job["id"])
    token = t.app.state.artifacts.token
    published = t.client.post(
        "/api/internal/consultation-artifacts",
        json={
            "session_id": "chat-session",
            "text": "готовый отчёт",
            "name": "report.txt",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "pending"
    assert body["download_url"].startswith(f"/api/chats/{chat['id']}/artifacts/")

    assert t.client.get(body["download_url"]).status_code == 410
    assert t.db.complete_chat_run(job["id"])
    ready = t.client.get(body["download_url"])
    assert ready.status_code == 200
    assert ready.content == "готовый отчёт".encode()
    login(t.client, "admin", "correct-horse-battery-staple")
    assert t.client.get(body["download_url"]).status_code == 200
    assert any(
        item["action"] == "chat.artifact.download" for item in t.db.list_audit()
    )


def test_dialog_messages_decorate_completed_artifact(dialog_api):
    t = dialog_api
    worker = t.users["worker"]
    folder = worker["workspace_path"]
    chat = t.db.create_chat(worker["id"], "chat-session", "Dialog", folder)
    job = t.db.create_job(
        chat=chat,
        effective_user_id=worker["id"],
        actor_user_id=worker["id"],
        content="Сделай отчёт",
        agent="build",
        provider_id="test",
        model_id="coder",
        model_variant=None,
    )
    t.db.set_job_status(job["id"], "running")
    artifact = t.app.state.artifacts.publish_text_for_job(
        chat["id"], job["id"], text="ready", name="report.txt"
    )
    assert t.db.complete_chat_run(job["id"])

    async def messages(_session_id):
        return [
            {"info": {"id": job["message_id"], "role": "user"}, "parts": []},
            {"info": {"id": "assistant-1", "role": "assistant"}, "parts": []},
        ]

    t.manager.client.messages = messages
    response = t.client.get(f"/api/chats/{chat['id']}/messages")
    assert response.status_code == 200, response.text
    items = response.json()
    assistant = next(item for item in items if item["info"]["role"] == "assistant")
    assert assistant["artifacts"][0]["id"] == artifact["id"]
    assert assistant["artifacts"][0]["download_url"].startswith(
        f"/api/chats/{chat['id']}/artifacts/"
    )
