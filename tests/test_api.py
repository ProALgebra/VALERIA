from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from opencode_web.app import create_app, safe_messages, safe_opencode_event
from opencode_web.config import Settings


class FakeOpenCodeClient:
    def __init__(self) -> None:
        self.session_count = 0
        self.session_records: dict[str, dict[str, Any]] = {}
        self.last_create_title: str | None = None

    async def create_session(self, title: str | None = None) -> dict[str, Any]:
        self.session_count += 1
        self.last_create_title = title
        session = {
            "id": f"session-{self.session_count}",
            "title": title or f"New session - {self.session_count}",
        }
        self.session_records[session["id"]] = session
        return session

    async def sessions(self) -> list[dict[str, Any]]:
        return list(self.session_records.values())

    async def session(self, session_id: str) -> dict[str, Any]:
        return self.session_records[session_id]

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return []

    async def providers(self) -> dict[str, Any]:
        return {
            "connected": ["test"],
            "all": [
                {
                    "id": "test",
                    "apiKey": "must-never-reach-the-browser",
                    "models": {"coder": {"name": "Test Coder", "secret": "hidden"}},
                }
            ],
        }

    async def permissions(self) -> list[dict[str, Any]]:
        return []

    async def mcp_statuses(self) -> dict[str, Any]:
        return {"memory": {"status": "connected"}, "gitlab": {"status": "disabled"}}


def test_model_catalog_only_contains_connected_providers() -> None:
    from opencode_web.app import safe_model_catalog

    payload = {
        "connected": ["connected"],
        "all": [
            {"id": "connected", "models": {"coder": {"name": "Coder"}}},
            {"id": "known-but-unconfigured", "models": {"other": {"name": "Other"}}},
        ],
    }

    assert safe_model_catalog(payload, ("*",)) == [
        {"id": "connected/coder", "name": "Coder", "provider": "connected"}
    ]


class FakeManager:
    def __init__(self) -> None:
        self.client = FakeOpenCodeClient()
        self.workspace: Path | None = None
        self.ensure_calls = 0

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True):
        yield self.client

    async def ensure(self, workspace: str) -> FakeOpenCodeClient:
        self.ensure_calls += 1
        self.workspace = Path(workspace)
        return self.client

    def instances(self) -> list[Any]:
        if not self.workspace:
            return []
        return [SimpleNamespace(workspace=self.workspace, client=self.client)]

    async def close(self) -> None:
        pass


def login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"login": login_name, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_runtime_payload_and_reasoning_are_preserved() -> None:
    messages = [
        {
            "info": {"id": "message", "tokens": {"reasoning": 42}},
            "parts": [
                {"type": "reasoning", "text": "Полная цепочка анализа"},
                {"type": "tool", "state": {"metadata": {"runtime": "visible"}}},
            ],
        }
    ]
    assert safe_messages(messages) == messages

    envelope = {
        "type": "opencode.event",
        "workspace": "/workspace",
        "event": {
            "directory": "/workspace",
            "payload": {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "session-1",
                    "part": messages[0]["parts"][0],
                },
            },
        },
    }
    projected = safe_opencode_event(envelope)
    assert projected["session_id"] == "session-1"
    assert projected["event"] == envelope["event"]


def test_auth_admin_templates_queue_and_impersonation(settings: Settings, workspace) -> None:
    user_workspace = workspace / "relay"
    user_workspace.mkdir()
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        app.state.manager = FakeManager()
        csrf = login(client, "admin", "correct-horse-battery-staple")
        headers = {"x-csrf-token": csrf}

        created = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "login": "ivanov",
                "password": "a-strong-user-password",
                "role": "user",
                "workspace_path": str(user_workspace),
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]

        template = client.post(
            "/api/admin/templates",
            headers=headers,
            json={"title": "CI", "content": "Проверь CI", "is_published": True},
        )
        assert template.status_code == 201
        template_id = template.json()["id"]
        edited = client.patch(
            f"/api/admin/templates/{template_id}",
            headers=headers,
            json={"description": "Шаблон для проверки CI"},
        )
        assert edited.status_code == 200
        assert edited.json()["description"] == "Шаблон для проверки CI"

        client.post("/api/auth/logout", headers=headers)
        user_csrf = login(client, "ivanov", "a-strong-user-password")
        user_headers = {"x-csrf-token": user_csrf}
        assert len(client.get("/api/templates").json()) == 1
        assert client.post("/api/admin/templates", headers=user_headers, json={}).status_code == 403
        options = client.get("/api/options").json()
        assert options["models"] == [{"id": "test/coder", "name": "Test Coder", "provider": "test"}]
        assert "must-never-reach-the-browser" not in str(options)
        public_config = client.get("/api/config/public").json()
        assert public_config["agents"] == ["build", "plan"]
        assert public_config["default_agent"] == "build"

        chat_result = client.post(
            "/api/chats",
            headers=user_headers,
            json={"title": "CI failure", "agent": "plan", "model": "test/coder"},
        )
        assert chat_result.status_code == 201, chat_result.text
        chat = chat_result.json()["chat"]
        chat_id = chat["id"]
        assert chat["title"] == "Новый диалог"
        assert app.state.manager.client.session_count == 0
        assert app.state.manager.client.last_create_title is None
        assert chat["preferred_agent"] == "plan"
        assert chat["preferred_provider_id"] == "test"
        assert chat["preferred_model_id"] == "coder"
        pinned_options = client.get(f"/api/options?chat_id={chat_id}").json()
        assert pinned_options["selected_agent"] == "plan"
        assert pinned_options["selected_model"] == "test/coder"
        # Empty chats stay local until their first queued turn starts.
        assert client.get(f"/api/chats/{chat_id}/mcp").json()["pending"] is True
        session = client.portal.call(app.state.manager.client.create_session)
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE chats SET opencode_session_id = ? WHERE id = ?",
                (session["id"], chat_id),
            )
        chat = app.state.database.get_chat(chat_id)
        mcp = client.get(f"/api/chats/{chat_id}/mcp")
        assert mcp.status_code == 200
        assert mcp.json()["servers"] == [
            {"name": "gitlab", "status": "disabled"},
            {"name": "memory", "status": "connected"},
        ]

        app.state.manager.client.session_records[chat["opencode_session_id"]]["title"] = (
            "OpenCode generated title"
        )
        ensure_calls = app.state.manager.ensure_calls
        assert client.get("/api/chats").json()[0]["title"] == "Новый диалог"
        assert app.state.manager.ensure_calls == ensure_calls
        title_updates = client.post("/api/chats/title-sync", headers=user_headers)
        assert title_updates.status_code == 200
        assert title_updates.json() == [{"id": chat_id, "title": "OpenCode generated title"}]
        assert client.post("/api/chats/title-sync", headers=user_headers).json() == []
        assert client.get(f"/api/chats/{chat_id}").json()["title"] == "OpenCode generated title"
        assert app.state.database.get_chat(chat_id)["title"] == "OpenCode generated title"

        client.post("/api/auth/logout", headers=user_headers)
        admin_csrf = login(client, "admin", "correct-horse-battery-staple")
        admin_headers = {"x-csrf-token": admin_csrf}
        impersonated = client.post(
            f"/api/admin/chats/{chat_id}/messages",
            headers=admin_headers,
            json={"message": "Проверь логи"},
        )
        assert impersonated.status_code == 202, impersonated.text
        job = impersonated.json()
        assert job["effective_user_id"] == user_id
        assert job["actor_user_id"] != user_id
        assert job["agent"] == "plan"
        assert job["provider_id"] == "test"
        assert job["model_id"] == "coder"

        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE queue_jobs SET content = ?, pipeline_json = ?, resume_content = ? "
                "WHERE id = ?",
                ("x" * 200_000, "y" * 200_000, "z" * 200_000, job["id"]),
            )
        queue_response = client.get("/api/admin/queue")
        queue = queue_response.json()
        assert queue["queued"] == 1
        assert queue["jobs"][0]["effective_login"] == "ivanov"
        assert queue["jobs"][0]["actor_login"] == "admin"
        assert queue["jobs"][0]["content"] == "x" * 90
        assert "pipeline_json" not in queue["jobs"][0]
        assert "resume_content" not in queue["jobs"][0]
        assert len(queue_response.content) < 20_000

        changed = client.patch(
            "/api/admin/settings/concurrency", headers=admin_headers, json={"value": 3}
        )
        assert changed.status_code == 200
        assert client.get("/api/admin/queue").json()["limit"] == 3
        assert client.get("/api/admin/mcp").json()[0]["servers"][1] == {
            "name": "memory",
            "status": "connected",
        }

        audit = client.get("/api/admin/audit").json()["items"]
        assert any(item["action"] == "message.impersonated" for item in audit)

        deleted = client.delete(f"/api/admin/templates/{template_id}", headers=admin_headers)
        assert deleted.status_code == 204


def test_admin_manages_priority_queues_and_default_chat_route(
    settings: Settings, workspace
) -> None:
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        csrf = login(client, "admin", "correct-horse-battery-staple")
        headers = {"x-csrf-token": csrf}
        created = client.post(
            "/api/admin/queues", headers=headers,
            json={"name": "Срочные", "priority": 100, "max_workers": 2},
        )
        assert created.status_code == 201, created.text
        queue = created.json()
        assert queue["name"] == "Срочные"
        assert client.put(
            "/api/admin/settings/default-chat-queue", headers=headers,
            json={"queue_id": queue["id"]},
        ).status_code == 200
        overview = client.get("/api/admin/queue").json()
        assert overview["default_chat_queue_id"] == queue["id"]
        assert {item["name"] for item in overview["queues"]} >= {"Основная", "Срочные"}
        user_workspace = workspace / "queue-user"
        user_workspace.mkdir()
        app.state.database.create_user(
            "queue-user", "long-enough-password", "user", str(user_workspace)
        )
        client.post("/api/auth/logout", headers=headers)
        user_headers = {
            "x-csrf-token": login(client, "queue-user", "long-enough-password")
        }
        chat = client.post(
            "/api/chats", headers=user_headers,
            json={"message": "Route me", "agent": "build", "model": "test/coder"},
        )
        assert chat.status_code == 201, chat.text
        assert chat.json()["job"]["queue_id"] == queue["id"]
        client.post("/api/auth/logout", headers=user_headers)
        headers = {
            "x-csrf-token": login(client, "admin", "correct-horse-battery-staple")
        }
        updated = client.patch(
            f"/api/admin/queues/{queue['id']}", headers=headers,
            json={"name": "Критические", "priority": 200, "max_workers": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["priority"] == 200
        # An assigned queue cannot disappear and silently reroute new work.
        assert client.delete(f"/api/admin/queues/{queue['id']}", headers=headers).status_code == 409
        assert client.post(
            "/api/admin/queues", headers=headers,
            json={
                "name": "Too wide", "priority": 0,
                "max_workers": settings.max_active_sessions_hard_limit + 1,
            },
        ).status_code == 422


def test_user_cannot_open_another_users_chat(settings: Settings, workspace) -> None:
    first_workspace = workspace / "one"
    second_workspace = workspace / "two"
    first_workspace.mkdir()
    second_workspace.mkdir()
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        app.state.manager = FakeManager()
        csrf = login(client, "admin", "correct-horse-battery-staple")
        headers = {"x-csrf-token": csrf}
        for name, path in (("one", first_workspace), ("two", second_workspace)):
            response = client.post(
                "/api/admin/users",
                headers=headers,
                json={
                    "login": name,
                    "password": f"strong-password-{name}",
                    "role": "user",
                    "workspace_path": str(path),
                },
            )
            assert response.status_code == 201
        client.post("/api/auth/logout", headers=headers)

        one_csrf = login(client, "one", "strong-password-one")
        chat = client.post(
            "/api/chats",
            headers={"x-csrf-token": one_csrf},
            json={"title": "Private"},
        ).json()["chat"]
        client.post("/api/auth/logout", headers={"x-csrf-token": one_csrf})

        login(client, "two", "strong-password-two")
        assert client.get(f"/api/chats/{chat['id']}").status_code == 404


def test_admin_user_creation_creates_missing_workspace(
    settings: Settings, workspace: Path
) -> None:
    missing_workspace = workspace / "new-team" / "project"
    app = create_app(settings, start_dispatcher=False)

    with TestClient(app) as client:
        csrf = login(client, "admin", "correct-horse-battery-staple")
        response = client.post(
            "/api/admin/users",
            headers={"x-csrf-token": csrf},
            json={
                "login": "new-worker",
                "password": "strong-new-worker-password",
                "role": "user",
                "workspace_path": "new-team/project",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["workspace_path"] == str(missing_workspace)
    assert missing_workspace.is_dir()


def test_admin_user_creation_rejects_missing_workspace_outside_allowed_root(
    settings: Settings, workspace: Path
) -> None:
    outside_workspace = workspace.parent / "outside" / "project"
    app = create_app(settings, start_dispatcher=False)

    with TestClient(app) as client:
        csrf = login(client, "admin", "correct-horse-battery-staple")
        response = client.post(
            "/api/admin/users",
            headers={"x-csrf-token": csrf},
            json={
                "login": "outside-worker",
                "password": "strong-outside-worker-password",
                "role": "user",
                "workspace_path": str(outside_workspace),
            },
        )

    assert response.status_code == 422
    assert "outside ALLOWED_WORKSPACE_ROOTS" in response.text
    assert not outside_workspace.exists()


def test_workspace_change_archives_old_chats(settings: Settings, workspace) -> None:
    old_workspace = workspace / "old"
    new_workspace = workspace / "new"
    old_workspace.mkdir()
    assert not new_workspace.exists()
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        app.state.manager = FakeManager()
        admin_csrf = login(client, "admin", "correct-horse-battery-staple")
        created_user = client.post(
            "/api/admin/users",
            headers={"x-csrf-token": admin_csrf},
            json={
                "login": "worker",
                "password": "strong-worker-password",
                "role": "user",
                "workspace_path": str(old_workspace),
            },
        ).json()
        client.post("/api/auth/logout", headers={"x-csrf-token": admin_csrf})

        user_csrf = login(client, "worker", "strong-worker-password")
        chat = client.post(
            "/api/chats",
            headers={"x-csrf-token": user_csrf},
            json={"title": "Old workspace"},
        ).json()["chat"]
        client.post("/api/auth/logout", headers={"x-csrf-token": user_csrf})

        admin_csrf = login(client, "admin", "correct-horse-battery-staple")
        changed = client.patch(
            f"/api/admin/users/{created_user['id']}",
            headers={"x-csrf-token": admin_csrf},
            json={"workspace_path": str(new_workspace)},
        )
        assert changed.status_code == 200, changed.text
        assert new_workspace.is_dir()
        client.post("/api/auth/logout", headers={"x-csrf-token": admin_csrf})

        login(client, "worker", "strong-worker-password")
        assert client.get("/api/chats").json() == []
        assert client.get(f"/api/chats/{chat['id']}").status_code == 404
