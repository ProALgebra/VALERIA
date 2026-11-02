from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from opencode_web.app import create_app
from opencode_web.database import ExecutionModeChangingError
from opencode_web.opencode_client import OpenCodeError
from opencode_web.podman_runtime import PodmanError, PodmanRuntime

PASSWORD = "correct-horse-battery-staple"
API = "/api/admin/settings/execution-mode"


class RuntimeManager:
    def __init__(self):
        self.execution_mode = "direct"
        self.calls = []
        self.changes = []
        self.histories = {}
        self.failure = None
        self.podman = SimpleNamespace(
            preflight=AsyncMock(), build_launch=lambda *args: None,
        )

    async def ensure(self, workspace, *, expected_mode=None):
        if expected_mode and expected_mode != self.execution_mode:
            raise OpenCodeError("Wrong runtime")
        self.calls.append((self.execution_mode, workspace))
        if self.failure:
            raise self.failure
        return self

    async def messages(self, session_id):
        return self.histories.get(session_id, [])

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True, expected_mode=None):
        yield await self.ensure(workspace, expected_mode=expected_mode)

    async def change_execution_mode(self, mode):
        self.changes.append(mode)
        self.execution_mode = mode

    def instances(self):
        return []

    async def close(self):
        pass


@pytest.fixture
def runtime_app(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(PodmanRuntime, "preflight", AsyncMock())
    app = create_app(
        replace(settings, podman_state_root=tmp_path / "private-state"), start_dispatcher=False,
    )
    with TestClient(app) as client:
        manager = RuntimeManager()
        app.state.manager = manager
        result = client.post("/api/auth/login", json={"login": "admin", "password": PASSWORD})
        assert result.status_code == 200
        headers = {"x-csrf-token": result.json()["csrf_token"]}
        yield SimpleNamespace(
            app=app, client=client, db=app.state.database, manager=manager, headers=headers,
        )


def owner_and_chat(t, workspace, *, session="pending_initial", consultation=False):
    folder = workspace / "project"
    folder.mkdir(exist_ok=True)
    user = t.db.create_user("worker", "long-worker-password", "user", str(folder))
    chat = t.db.create_chat(
        user["id"], session, "Saved dialog", str(folder), consultation=consultation,
    )
    return user, chat


def test_mode_is_admin_only_and_requires_csrf(runtime_app, workspace):
    t = runtime_app
    assert t.client.get(API).json() == {
        "mode": "direct", "availability": {"available": True, "reason": None},
        "can_change": True,
    }
    assert t.client.put(API, json={"mode": "podman"}).status_code == 403
    assert t.client.put(API, headers=t.headers, json={"mode": "unknown"}).status_code == 422
    owner_and_chat(t, workspace)
    t.client.post("/api/auth/login", json={"login": "worker", "password": "long-worker-password"})
    assert t.client.get(API).status_code == 403
    t.client.cookies.clear()
    assert t.client.get(API).status_code == 401


def test_switch_persists_and_restores_separate_histories(runtime_app, workspace):
    t = runtime_app
    _, chat = owner_and_chat(t, workspace, session="ses-direct")
    messages = [{"info": {"id": "answer", "role": "assistant"}, "parts": [
        {"type": "text", "text": "Original direct history"},
    ]}]
    t.manager.histories["ses-direct"] = messages
    switched = t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["mode"] == t.db.execution_mode() == "podman"
    assert t.manager.changes == ["podman"]
    assert t.db.get_chat(chat["id"])["archived_reason"] == "execution_mode_changed"
    before = list(t.manager.calls)
    assert t.client.get(f"/api/chats/{chat['id']}/messages").json() == messages
    assert t.client.get(f"/api/chats/{chat['id']}").status_code == 200
    assert t.client.get(f"/api/options?chat_id={chat['id']}").status_code == 200
    assert t.client.get(f"/api/chats/{chat['id']}/questions").json() == []
    assert t.client.get(f"/api/chats/{chat['id']}/permissions").json() == []
    assert t.client.get(f"/api/chats/{chat['id']}/mcp").json()["archived"]
    assert t.manager.calls == before  # No old session ID is sent to the new runtime.
    rejected = t.client.post(
        f"/api/chats/{chat['id']}/messages", headers=t.headers,
        json={"message": "Must not run"},
    )
    assert rejected.status_code == 409
    podman_chat = t.db.create_chat(
        chat["owner_user_id"], None, "Isolated", chat["workspace_snapshot"],
    )
    assert podman_chat["execution_mode"] == "podman"
    assert t.client.put(API, headers=t.headers, json={"mode": "direct"}).status_code == 200
    assert t.db.get_chat(chat["id"])["archived_reason"] is None
    assert t.db.get_chat(podman_chat["id"])["archived_reason"] == "execution_mode_changed"
    assert [row["action"] for row in t.db.list_audit()].count("execution_mode.update") == 2
    t.db.initialize()
    assert t.db.execution_mode() == "direct"


@pytest.mark.parametrize("state", [
    "queued", "dispatching", "running", "waiting_permission", "waiting_question", "cancelling",
])
def test_switch_rejects_every_unfinished_job(runtime_app, workspace, state):
    t = runtime_app
    user, chat = owner_and_chat(t, workspace)
    job = t.db.create_job(
        chat=chat, effective_user_id=user["id"], actor_user_id=user["id"], content="Work",
        agent="build", provider_id=None, model_id=None, model_variant=None,
    )
    t.db.set_job_status(job["id"], state)
    assert not t.client.get(API).json()["can_change"]
    result = t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert result.status_code == 409
    assert t.db.execution_mode() == "direct"
    t.manager.podman.preflight.assert_not_awaited()
    assert t.manager.changes == []


def test_failed_podman_readiness_does_not_change_or_archive(runtime_app, workspace):
    t = runtime_app
    _, chat = owner_and_chat(t, workspace)
    t.manager.podman.preflight.side_effect = PodmanError("Podman is not ready")
    result = t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert result.status_code == 409
    assert t.db.execution_mode() == "direct"
    assert t.db.get_chat(chat["id"])["archived_reason"] is None
    t.db.assert_execution_mode_ready()
    assert t.manager.changes == []


def test_history_failure_keeps_original_mode(runtime_app, workspace):
    t = runtime_app
    _, chat = owner_and_chat(t, workspace, session="ses-missing")
    t.manager.failure = OpenCodeError("Unavailable")
    result = t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert result.status_code == 409
    assert t.db.execution_mode() == "direct"
    assert t.db.get_chat(chat["id"])["archived_reason"] is None
    t.db.assert_execution_mode_ready()


def test_transition_freezes_submissions_until_snapshot_finishes(runtime_app, workspace):
    t = runtime_app
    user, chat = owner_and_chat(t, workspace, session="ses-existing")

    async def read_history(session_id):
        with pytest.raises(ExecutionModeChangingError):
            t.db.create_job(
                chat=chat, effective_user_id=user["id"], actor_user_id=user["id"],
                content="Racing submission", agent="build", provider_id=None,
                model_id=None, model_variant=None,
            )
        return []

    t.manager.messages = read_history
    assert t.client.put(API, headers=t.headers, json={"mode": "podman"}).status_code == 200
    assert not t.db.has_unfinished_jobs()
    t.db.assert_execution_mode_ready()


def test_isolation_rejects_nested_workspaces_even_when_sharing_is_allowed(runtime_app, workspace):
    t = runtime_app
    owner_and_chat(t, workspace)
    nested = workspace / "project" / "nested"
    nested.mkdir()
    t.db.create_user("other", "another-long-password", "user", str(nested))
    result = t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert result.status_code == 409
    assert "не вложенные" in result.json()["detail"]
    assert t.db.execution_mode() == "direct"


def test_consultation_archive_metadata_and_original_answers_survive(runtime_app, workspace):
    t = runtime_app
    _, chat = owner_and_chat(t, workspace, consultation=True)
    assert t.client.put(API, headers=t.headers, json={"mode": "podman"}).status_code == 200
    detail = t.client.get(f"/api/admin/consultations/{chat['id']}").json()
    assert detail["archived_reason"] == "execution_mode_changed"
    assert detail["execution_mode"] == "direct"
    assert t.client.put(API, headers=t.headers, json={"mode": "direct"}).status_code == 200
    assert t.client.get(f"/api/admin/consultations/{chat['id']}").json()["archived_reason"] is None


def test_persisted_podman_mode_is_loaded_at_startup(settings):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app):
        db = app.state.database
        admin = db.get_user_by_login("admin")
        db.set_execution_mode("podman", admin["id"])
    with TestClient(create_app(settings, start_dispatcher=False)) as client:
        assert client.app.state.manager.execution_mode == "podman"


def test_startup_reconciles_persisted_podman_even_with_an_empty_queue(settings, monkeypatch):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app):
        db = app.state.database
        db.set_execution_mode("podman", db.get_user_by_login("admin")["id"])
    reconcile = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr(PodmanRuntime, "reconcile", reconcile)
    monkeypatch.setattr(PodmanRuntime, "start", start)
    with TestClient(create_app(settings, start_dispatcher=False)) as client:
        reconcile.assert_awaited_once()
        start.assert_not_awaited()
        assert client.app.state.manager.execution_mode == "podman"
        assert client.app.state.manager.instances() == []
        assert not client.app.state.database.has_unfinished_jobs()


def test_failed_startup_reconciliation_preserves_admin_access_without_fallback(
    settings, monkeypatch,
):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app):
        db = app.state.database
        db.set_execution_mode("podman", db.get_user_by_login("admin")["id"])
    monkeypatch.setattr(PodmanRuntime, "preflight", AsyncMock())
    monkeypatch.setattr(
        PodmanRuntime, "reconcile", AsyncMock(side_effect=PodmanError("Engine unavailable")),
    )
    start = AsyncMock()
    monkeypatch.setattr(PodmanRuntime, "start", start)
    with TestClient(create_app(settings, start_dispatcher=False)) as client:
        result = client.post("/api/auth/login", json={"login": "admin", "password": PASSWORD})
        assert result.status_code == 200
        assert client.get(API).json()["mode"] == "podman"
        manager = client.app.state.manager
        with pytest.raises(OpenCodeError, match="Engine unavailable"):
            client.portal.call(manager.ensure, str(settings.allowed_workspace_roots[0]))
        assert manager.execution_mode == "podman"
        start.assert_not_awaited()


def test_workspace_assignment_rechecks_mode_after_legacy_precheck(runtime_app, workspace):
    t = runtime_app
    user, chat = owner_and_chat(t, workspace)
    nested = workspace / "project" / "nested"
    nested.mkdir()
    # A request checked this path in direct mode, then awaited an external service.
    assert t.db.execution_mode() == "direct"
    assert t.client.put(API, headers=t.headers, json={"mode": "podman"}).status_code == 200
    with pytest.raises(ValueError, match="не должны пересекаться"):
        t.db.create_user("other", "another-long-password", "user", str(nested))
    other_path = workspace / "other"
    other_path.mkdir()
    other = t.db.create_user("other", "another-long-password", "user", str(other_path))
    with pytest.raises(ValueError, match="не должны пересекаться"):
        t.db.update_user(other["id"], workspace_path=chat["workspace_snapshot"])
    assert t.db.get_user(user["id"])["workspace_path"] == chat["workspace_snapshot"]
    assert t.db.get_user(other["id"])["workspace_path"] == str(other_path)


def test_workspace_change_rechecks_newly_queued_jobs_inside_transaction(runtime_app, workspace):
    t = runtime_app
    user, chat = owner_and_chat(t, workspace)
    assert not t.db.has_pending_jobs(user["id"])
    t.db.create_job(
        chat=chat, effective_user_id=user["id"], actor_user_id=user["id"], content="Work",
        agent="build", provider_id=None, model_id=None, model_variant=None,
    )
    with pytest.raises(ValueError, match="при незавершённых заданиях"):
        t.db.update_user(user["id"], workspace_path=str(workspace / "other"))
    assert t.db.get_user(user["id"])["workspace_path"] == chat["workspace_snapshot"]


def test_database_failure_rolls_back_runtime_and_metadata(runtime_app, workspace, monkeypatch):
    t = runtime_app
    _, chat = owner_and_chat(t, workspace)
    original = t.db.audit

    def audit(actor, action, **kwargs):
        if action == "execution_mode.update":
            raise RuntimeError("Storage failed")
        return original(actor, action, **kwargs)

    monkeypatch.setattr(t.db, "audit", audit)
    with pytest.raises(RuntimeError, match="Storage failed"):
        t.client.put(API, headers=t.headers, json={"mode": "podman"})
    assert t.db.execution_mode() == t.manager.execution_mode == "direct"
    assert t.db.get_chat(chat["id"])["archived_reason"] is None
    t.db.assert_execution_mode_ready()
