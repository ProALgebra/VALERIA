from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import FakeManager, login

from opencode_web.app import create_app


@pytest.fixture
def admin_session(settings):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        app.state.manager = FakeManager()
        csrf = login(client, "admin", "correct-horse-battery-staple")
        admin = client.get("/api/auth/me").json()["user"]
        yield SimpleNamespace(
            app=app,
            client=client,
            admin=admin,
            headers={"x-csrf-token": csrf},
            path=f"/api/admin/users/{admin['id']}",
        )


def test_bootstrap_admin_assigns_own_workspace_without_logging_in_again(
    admin_session, workspace: Path,
) -> None:
    t = admin_session
    assert t.admin["workspace_path"] is None
    before = t.client.post("/api/chats", headers=t.headers, json={})
    assert before.status_code == 422

    assigned = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": "admin/project"},
    )
    folder = workspace / "admin" / "project"
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["workspace_path"] == str(folder)
    assert assigned.json()["role"] == "admin"
    assert folder.is_dir()

    me = t.client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["workspace_path"] == str(folder)
    assert me.json()["csrf_token"] == t.headers["x-csrf-token"]
    assert t.client.get("/api/config/public").json()["workspace"] == str(folder)
    created = t.client.post("/api/chats", headers=t.headers, json={})
    assert created.status_code == 201, created.text
    assert created.json()["chat"]["owner_user_id"] == t.admin["id"]
    assert created.json()["chat"]["workspace_snapshot"] == str(folder)


def test_bootstrap_admin_cannot_assign_workspace_outside_allowed_roots(
    admin_session, workspace: Path,
) -> None:
    t = admin_session
    folder = workspace.parent / "outside" / "admin"
    response = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": str(folder)},
    )
    assert response.status_code == 422
    assert "outside ALLOWED_WORKSPACE_ROOTS" in response.text
    assert not folder.exists()
    assert t.client.get("/api/auth/me").json()["user"]["workspace_path"] is None


def test_bootstrap_admin_cannot_assign_another_users_workspace(
    admin_session, workspace: Path,
) -> None:
    t = admin_session
    folder = workspace / "worker"
    folder.mkdir()
    t.app.state.database.create_user(
        "worker", "long-enough-password", "user", str(folder),
    )
    response = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": "worker"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is already assigned"
    assert t.client.get("/api/auth/me").json()["user"]["workspace_path"] is None


@pytest.mark.parametrize(
    "job_status",
    ["queued", "dispatching", "running", "waiting_permission", "waiting_question", "cancelling"],
)
def test_bootstrap_admin_workspace_change_waits_for_own_pending_jobs(
    admin_session, workspace: Path, job_status: str,
) -> None:
    t = admin_session
    assigned = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": "admin/old"},
    )
    assert assigned.status_code == 200, assigned.text
    created = t.client.post(
        "/api/chats", headers=t.headers, json={"message": "Work in the current folder"},
    )
    assert created.status_code == 201, created.text
    chat = created.json()["chat"]
    job = created.json()["job"]
    db = t.app.state.database
    db.set_job_status(job["id"], job_status)

    folder = workspace / "admin" / "new"
    response = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": "admin/new"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace cannot change while jobs are queued or active"
    assert not folder.exists()
    assert db.get_user(t.admin["id"])["workspace_path"] == str(workspace / "admin" / "old")
    assert db.get_chat(chat["id"])["archived_reason"] is None
    assert db.get_job(job["id"])["status"] == job_status

    db.set_job_status(job["id"], "cancelled" if job_status == "cancelling" else "completed")
    changed = t.client.patch(
        t.path, headers=t.headers, json={"workspace_path": "admin/new"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["workspace_path"] == str(folder)
    assert folder.is_dir()
    assert db.get_chat(chat["id"])["archived_reason"] == "workspace_changed"
    assert t.client.get("/api/chats").json() == []
