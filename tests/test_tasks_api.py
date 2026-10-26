from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from opencode_web.app import create_app, event_for_user, public_task_run
from opencode_web.config import ConfigError
from opencode_web.notifications import MattermostNotificationError


class TaskClient:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.generated_titles = {}

    async def create_session(self, title=None):
        session = {"id": f"session-{len(self.created)}", "title": title}
        self.created.append(session)
        return session

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        return True

    async def sessions(self):
        return [
            {
                "id": session["id"],
                "title": self.generated_titles.get(session["id"], session["title"]),
            }
            for session in self.created
        ]

    async def providers(self):
        return {"all": []}

    async def messages(self, session_id):
        return [
            {
                "info": {"role": "assistant", "sessionID": session_id},
                "parts": [{"type": "text", "text": "SECRET TASK OUTPUT"}],
            }
        ]


class TaskManager:
    def __init__(self):
        self.client = TaskClient()

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True):
        assert wake is False
        assert activity is False
        yield self.client

    async def ensure(self, workspace):
        return self.client

    async def close(self):
        pass


async def attach_run(t, run):
    """Simulate dispatch before testing upstream history and generated titles."""
    job = t.db.get_job(run["id"])
    t.db.set_job_status(job["id"], "dispatching")
    session = await t.manager.client.create_session(title=job["task_initial_title"])
    assert t.db.attach_task_session(job["id"], session["id"])
    return t.db.get_job(job["id"])


@pytest.fixture
async def task_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        manager = TaskManager()
        app.state.manager = manager
        db = app.state.database
        users = {}
        for name, only in (("worker", False), ("tasker", True), ("stranger", False)):
            path = workspace / name
            path.mkdir()
            users[name] = db.create_user(
                name, "a-strong-password", "user", str(path), tasks_only=only
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(app=app, client=client, db=db, users=users, manager=manager)


async def login(client, name="admin"):
    password = "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    response = await client.post("/api/auth/login", json={"login": name, "password": password})
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return response.json()["user"]


async def create_task(client, **fields):
    await login(client)
    response = await client.post(
        "/api/admin/tasks",
        json={
            "title": "Проверка CI",
            "description": "Проверяет сборку",
            "content": "SECRET ADMIN INSTRUCTIONS",
            "agent": "build",
            "model": "provider/coder",
            "model_variant": "high",
            "is_published": True,
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_task_queue_assignment_is_snapshotted_for_new_runs(task_app):
    t = task_app
    await login(t.client)
    queue_response = await t.client.post(
        "/api/admin/queues",
        json={"name": "CI urgent", "priority": 50, "max_workers": 1},
    )
    assert queue_response.status_code == 201, queue_response.text
    queue_id = queue_response.json()["id"]
    task = await create_task(t.client, queue_id=queue_id)
    await login(t.client, "worker")
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    assert t.db.get_job(run["id"])["queue_id"] == queue_id

    await login(t.client)
    changed = await t.client.patch(
        f"/api/admin/tasks/{task['id']}", json={"queue_id": "default"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["queue_id"] == "default"
    assert t.db.get_job(run["id"])["queue_id"] == queue_id


@pytest.mark.parametrize("username", ["worker", "tasker"])
async def test_task_queue_and_work_status_in_api_and_events(task_app, username):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, username)
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    assert run["status"] == "queued"
    for internal, visible in [
        ("queued", "queued"),
        ("dispatching", "dispatching"),
        ("running", "running"),
        ("waiting_permission", "waiting_permission"),
        ("waiting_question", "waiting_question"),
        ("running", "running"),
        ("completed", "completed"),
    ]:
        t.db.set_job_status(run["id"], internal)
        detail = (await t.client.get(f"/api/task-runs/{run['id']}")).json()
        assert detail["status"] == visible
        assert (await t.client.get("/api/task-runs")).json() == [detail]
        event = {"type": "job.updated", "job": t.db.get_job(run["id"])}
        assert event_for_user(t.db, t.users[username], event) == {
            "type": "task.updated", "task": detail,
        }
        assert event_for_user(t.db, t.users["stranger"], event) is None
        await login(t.client)
        for path in (
            "/api/admin/task-runs", f"/api/admin/users/{t.users[username]['id']}/task-runs"
        ):
            assert (await t.client.get(path)).json()[0]["status"] == visible
        await login(t.client, username)


@pytest.mark.parametrize("username", ["worker", "tasker"])
async def test_task_launch_privacy_and_fixed_configuration(task_app, username):
    t = task_app
    task = await create_task(t.client)
    await create_task(t.client, title="Черновик", is_published=False)
    await login(t.client, username)
    catalog = await t.client.get("/api/tasks")
    assert catalog.json() == [
        {
            "id": task["id"],
            "title": task["title"],
            "description": task["description"],
            "plan_first": 0,
            "input_hint": None,
        }
    ]
    response = await t.client.post(f"/api/tasks/{task['id']}/runs", json={"user_input": "my input"})
    assert response.status_code == 202, response.text
    run = response.json()
    assert set(run) == {
        "id", "task_id", "title", "status", "created_at", "started_at", "finished_at",
        "plan_first", "phase", "user_input", "error_code",
    }
    assert run["status"] == "queued"
    assert (run["user_input"], run["error_code"], run["started_at"]) == ("my input", None, None)
    job = t.db.get_job(run["id"])
    assert job["content"] == "SECRET ADMIN INSTRUCTIONS\n\nmy input"
    assert (job["agent"], job["provider_id"], job["model_id"], job["model_variant"]) == (
        "build",
        "provider",
        "coder",
        "high",
    )
    assert (await t.client.get("/api/task-runs")).json() == [run]
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).json() == run
    assert "SECRET" not in response.text
    for suffix in ("", "/messages", "/permissions", "/mcp"):
        assert (await t.client.get(f"/api/chats/{job['chat_id']}{suffix}")).status_code in {
            403,
            404,
        }
    for path in (f"/api/chats/{job['chat_id']}/messages", f"/api/chats/{job['chat_id']}/abort"):
        assert (await t.client.post(path, json={"message": "again"})).status_code in {403, 404}
    assert (await t.client.get(f"/api/jobs/{job['id']}")).status_code in {403, 404}
    assert (await t.client.delete(f"/api/jobs/{job['id']}")).status_code in {403, 404}
    queue = await t.client.get("/api/queue/me")
    assert queue.status_code == 200
    assert "SECRET" not in queue.text and job["session_id"] not in queue.text
    assert queue.json() == [{
        "id": run["id"], "chat_id": job["chat_id"], "kind": "task", "status": "queued",
        "title": run["title"], "task_id": task["id"], "queue_id": job["queue_id"],
        "queue_name": job["queue_name"], "created_at": run["created_at"], "started_at": None,
        "finished_at": None, "error_code": None, "position": 1,
    }]
    if username == "worker":
        assert (await t.client.get("/api/chats")).json() == []
    else:
        for path in ("/api/chats", "/api/options", "/api/templates"):
            assert (await t.client.get(path)).status_code == 403
        assert (await t.client.post("/api/chats", json={"message": "chat"})).status_code == 403
    t.db.set_job_status(job["id"], "failed", "secret error detail")
    failed = await t.client.get(f"/api/task-runs/{run['id']}")
    assert failed.json()["status"] == "failed"
    # Free-form failure text never leaves the server; machine codes do.
    assert failed.json()["error_code"] == "internal_error"
    assert "secret error" not in failed.text
    assert "secret error" not in (await t.client.get("/api/queue/me")).text
    # A late completion cannot turn a failed branch into a successful pipeline.
    t.db.set_job_status(job["id"], "completed")
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).json()["status"] == "failed"
    repeated = await t.client.post(f"/api/tasks/{task['id']}/runs", json={"user_input": ""})
    assert repeated.status_code == 202
    t.db.set_job_status(repeated.json()["id"], "completed")
    completed = await t.client.get(f"/api/task-runs/{repeated.json()['id']}")
    assert completed.json()["status"] == "completed"
    await login(t.client, "stranger")
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).status_code == 404
    assert (await t.client.get("/api/task-runs")).json() == []


async def test_admin_edit_delete_snapshot_and_user_restrictions(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    for path in ("/api/admin/tasks", "/api/admin/task-runs"):
        assert (await t.client.get(path)).status_code == 403
    assert (await t.client.post("/api/admin/tasks", json={})).status_code == 403
    assert (await t.client.patch(f"/api/admin/tasks/{task['id']}", json={})).status_code == 403
    assert (await t.client.delete(f"/api/admin/tasks/{task['id']}")).status_code == 403
    assert (
        await t.client.post(
            f"/api/tasks/{task['id']}/runs",
            json={"model": "other/model", "content": "override", "agent": "plan"},
        )
    ).status_code == 422
    csrf = t.client.headers.pop("x-csrf-token")
    assert (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).status_code == 403
    t.client.headers["x-csrf-token"] = csrf
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    await login(t.client)
    edit = await t.client.patch(
        f"/api/admin/tasks/{task['id']}",
        json={
            "content": "NEW SECRET",
            "model": "provider/other",
            "agent": "plan",
            "title": "New task",
        },
    )
    assert edit.status_code == 200, edit.text
    job = t.db.get_job(run["id"])
    assert job["content"] == "SECRET ADMIN INSTRUCTIONS"
    assert job["model_id"] == "coder"
    assert public_task_run(job)["title"] == task["title"]
    assert (await t.client.get("/api/admin/task-runs")).json()[0]["owner_login"] == "worker"
    for fields in (
        {"model": "/bad"},
        {"content": "  "},
        {"agent": "wrong"},
        {"model": None},
        {"is_published": None},
    ):
        assert (
            await t.client.patch(f"/api/admin/tasks/{task['id']}", json=fields)
        ).status_code == 422
    assert (await t.client.delete(f"/api/admin/tasks/{task['id']}")).status_code == 204
    await login(t.client, "worker")
    assert (await t.client.get("/api/tasks")).json() == []
    assert (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).status_code == 404
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).status_code == 200


async def test_tasks_only_create_update_and_existing_session_enforcement(task_app):
    t = task_app
    await login(t.client)
    new = await t.client.post(
        "/api/admin/users",
        json={
            "login": "limited",
            "password": "a-strong-password",
            "role": "user",
            "workspace_path": "limited",
            "tasks_only": True,
        },
    )
    assert new.status_code == 201, new.text
    assert new.json()["tasks_only"] is True
    worker = t.users["worker"]
    await login(t.client, "worker")
    assert (await t.client.get("/api/chats")).status_code == 200
    t.db.update_user(worker["id"], tasks_only=True)
    response = await t.client.get("/api/chats")
    assert response.status_code in {401, 403}
    await login(t.client)
    updated = await t.client.patch(
        f"/api/admin/users/{new.json()['id']}", json={"tasks_only": False}
    )
    assert updated.status_code == 200
    assert updated.json()["tasks_only"] is False


async def test_sse_projection_never_exposes_task_prompt_or_output(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    job = t.db.get_job(run["id"])
    owner = t.users["worker"]
    event = {"type": "job.updated", "job": job}
    assert event_for_user(t.db, owner, event) == {"type": "task.updated", "task": run}
    assert event_for_user(t.db, t.users["stranger"], event) is None
    admin_event = event_for_user(t.db, t.db.get_user_by_login("admin"), event)
    assert admin_event == {
        "type": "task.updated", "task": run, "owner_user_id": owner["id"]
    }
    for kind in ("message.part.updated", "question.asked", "session.updated", "session.error"):
        raw = {
            "type": "opencode.event",
            "workspace": owner["workspace_path"],
            "session_id": job["session_id"],
            "event": {"payload": {"type": kind, "properties": {"text": "SECRET OUTPUT"}}},
        }
        assert event_for_user(t.db, owner, raw) is None
    normal = t.db.create_chat(owner["id"], "ordinary", "Chat", owner["workspace_path"])
    raw["session_id"] = normal["opencode_session_id"]
    assert event_for_user(t.db, owner, raw) == raw
    assert event_for_user(t.db, {**owner, "tasks_only": True}, raw) is None


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("username", ["worker", "tasker"])
async def test_duplicate_run_is_cancelled_and_published_without_dispatch(
    task_app, settings, monkeypatch, status, username
):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    endpoint = f"/api/tasks/{task['id']}/runs"
    original = (await t.client.post(endpoint, json={"user_input": "Pipeline 123"})).json()
    t.db.set_job_status(original["id"], status)
    original_job = t.db.get_job(original["id"])
    await login(t.client, username)
    wake = Mock()
    publish = AsyncMock()
    monkeypatch.setattr(t.app.state.dispatcher, "wake", wake)
    monkeypatch.setattr(t.app.state.events, "publish", publish)
    # Duplicates must still be recorded when the queue's capacity is exhausted.
    monkeypatch.setattr(t.db, "queued_count", lambda: settings.max_queued_messages)
    response = await t.client.post(endpoint, json={"user_input": "  Pipeline 123\n"})
    assert response.status_code == 202, response.text
    duplicate = response.json()
    assert duplicate["id"] != original["id"]
    assert duplicate["status"] == "cancelled"
    assert duplicate["finished_at"] is not None
    assert set(duplicate) == set(original)
    assert t.db.get_job(original["id"]) == original_job
    duplicate_job = t.db.get_job(duplicate["id"])
    assert duplicate_job["status"] == "cancelled"
    assert duplicate_job["started_at"] is None
    assert duplicate_job["error_code"] == "duplicate_task"
    wake.assert_not_called()
    publish.assert_awaited_once_with({"type": "job.updated", "job": duplicate_job})
    visible = event_for_user(
        t.db, t.users[username], {"type": "job.updated", "job": duplicate_job}
    )
    assert visible == {"type": "task.updated", "task": duplicate}
    assert (await t.client.get(f"/api/task-runs/{duplicate['id']}")).json() == duplicate
    assert (await t.client.get("/api/task-runs")).json()[0] == duplicate
    if status == "queued":
        assert t.db.claim_next_job(10)["id"] == original["id"]
    assert t.db.claim_next_job(10) is None


async def test_draft_and_full_queue_do_not_create_upstream_sessions(task_app, settings):
    t = task_app
    draft = await create_task(t.client, is_published=False)
    task = await create_task(t.client)
    await login(t.client, "worker")
    assert (await t.client.post(f"/api/tasks/{draft['id']}/runs", json={})).status_code == 404
    object.__setattr__(settings, "max_queued_messages", 0)
    assert (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).status_code == 429
    assert t.manager.client.created == []


@pytest.mark.parametrize(
    "change,expected", [("task", 409), ("unpublish", 404), ("user", 409), ("capacity", 429)]
)
async def test_launch_rechecks_settings_atomically(task_app, change, expected):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    create_job = t.db.create_task_job

    def changed_job(**kwargs):
        if change == "task":
            t.db.update_task(task["id"], {"content": "changed by admin"})
        elif change == "unpublish":
            t.db.update_task(task["id"], {"is_published": False})
        elif change == "user":
            t.db.update_user(t.users["worker"]["id"], status="disabled")
        else:
            # Simulate another request filling the queue after the initial check.
            raise ValueError("The queue is full")
        return create_job(**kwargs)

    t.db.create_task_job = changed_job
    response = await t.client.post(f"/api/tasks/{task['id']}/runs", json={})
    assert response.status_code == expected, response.text
    assert t.manager.client.created == []
    assert t.manager.client.deleted == []
    assert t.db.list_task_runs() == []
    assert t.db._all("SELECT * FROM chats") == []


async def test_admin_can_view_any_task_run_content_users_cannot(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    job = await attach_run(t, run)
    messages = [
        {
            "info": {"role": "assistant", "sessionID": job["session_id"]},
            "parts": [{"type": "text", "text": "SECRET TASK OUTPUT"}],
        }
    ]
    path = f"/api/admin/task-runs/{run['id']}/messages"
    assert (await t.client.get(path)).status_code == 403
    await login(t.client, "stranger")
    assert (await t.client.get(path)).status_code == 403
    await login(t.client)
    response = await t.client.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == messages
    assert (await t.client.get("/api/admin/task-runs/missing/messages")).status_code == 404
    worker = t.users["worker"]
    audits = [
        item
        for item in t.db.list_audit()
        if item["action"] == "task_run.messages" and item["object_id"] == run["id"]
    ]
    assert audits and audits[0]["effective_user_id"] == worker["id"]


async def test_task_run_titles_sync_like_dialogs(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    assert run["title"] == task["title"]
    job = await attach_run(t, run)
    t.manager.client.generated_titles[job["session_id"]] = "Generated run name"
    synced = await t.client.post("/api/task-runs/title-sync")
    assert synced.status_code == 200, synced.text
    assert synced.json() == [{"id": run["id"], "title": f"{task['title']}: Generated run name"}]
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).json()["title"] == (
        f"{task['title']}: Generated run name"
    )
    assert (await t.client.get("/api/task-runs")).json()[0]["title"] == (
        f"{task['title']}: Generated run name"
    )
    assert t.db.get_job(run["id"])["task_title"] == task["title"]
    assert t.db.get_job(run["id"])["task_initial_title"] == task["title"]
    await login(t.client, "stranger")
    assert (await t.client.post("/api/task-runs/title-sync")).json() == []
    assert (await t.client.get("/api/task-runs")).json() == []
    await login(t.client)
    t.manager.client.generated_titles[job["session_id"]] = "Admin renamed run"
    assert (await t.client.post("/api/admin/task-runs/title-sync")).json() == [
        {"id": run["id"], "title": f"{task['title']}: Admin renamed run"}
    ]
    assert (await t.client.get("/api/admin/task-runs")).json()[0]["title"] == (
        f"{task['title']}: Admin renamed run"
    )


async def test_task_title_sync_skips_workspace_outside_roots(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    good = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    good_job = await attach_run(t, good)
    t.manager.client.generated_titles[good_job["session_id"]] = "Generated good"
    legacy = t.db.create_user(
        "legacy", "a-strong-password", "user", "/home/appuser/workspaces"
    )
    t.db.create_task_job(
        task=task,
        user=legacy,
        actor_user_id=legacy["id"],
        session_id="legacy-session",
        user_input="",
        max_queued=100,
    )
    original_ensure = t.manager.ensure

    async def ensure(workspace):
        if workspace == legacy["workspace_path"]:
            raise ConfigError("Workspace is outside ALLOWED_WORKSPACE_ROOTS")
        return await original_ensure(workspace)

    t.manager.ensure = ensure
    await login(t.client)
    response = await t.client.post("/api/admin/task-runs/title-sync")
    assert response.status_code == 200, response.text
    assert response.json() == [{"id": good["id"], "title": f"{task['title']}: Generated good"}]


@pytest.mark.parametrize("stored_title", [None, "", "Legacy run", "Проверка CI: Legacy run"])
async def test_existing_task_titles_keep_template_snapshot_in_api_and_events(
    task_app, stored_title
):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    job = t.db.get_job(run["id"])
    if stored_title is not None:
        t.db.set_chat_title(job["chat_id"], stored_title)
    t.db.update_task(task["id"], {"title": "Renamed template"})
    t.db.delete_task(task["id"])
    expected = f"{task['title']}: Legacy run" if stored_title else task["title"]
    assert (await t.client.get("/api/task-runs")).json()[0]["title"] == expected
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).json()["title"] == expected
    job = t.db.get_job(run["id"])
    event = {"type": "job.updated", "job": job}
    visible = event_for_user(t.db, t.users["worker"], event)
    assert visible == {"type": "task.updated", "task": {**run, "title": expected}}
    assert event_for_user(t.db, t.users["stranger"], event) is None
    await login(t.client)
    assert (await t.client.get("/api/admin/task-runs")).json()[0]["title"] == expected
    queue = (await t.client.get("/api/admin/queue")).json()
    assert queue["jobs"][0]["chat_title"] == expected


async def test_task_visibility_all_by_default_then_restricted(task_app):
    t = task_app
    task = await create_task(t.client)
    assert task["visibility"] == "all"
    assert task["audience_user_ids"] == []
    worker = t.users["worker"]
    await login(t.client, "worker")
    assert [item["id"] for item in (await t.client.get("/api/tasks")).json()] == [task["id"]]
    await login(t.client, "stranger")
    assert [item["id"] for item in (await t.client.get("/api/tasks")).json()] == [task["id"]]
    await login(t.client)
    patched = await t.client.patch(
        f"/api/admin/tasks/{task['id']}",
        json={"visibility": "selected", "user_ids": [worker["id"]]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["visibility"] == "selected"
    assert patched.json()["audience_user_ids"] == [worker["id"]]
    await login(t.client, "worker")
    assert [item["id"] for item in (await t.client.get("/api/tasks")).json()] == [task["id"]]
    assert (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).status_code == 202
    await login(t.client, "stranger")
    assert (await t.client.get("/api/tasks")).json() == []
    assert (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).status_code == 404


async def test_task_audience_endpoint_validation(task_app):
    t = task_app
    task = await create_task(t.client)
    worker = t.users["worker"]
    base = f"/api/admin/tasks/{task['id']}/audience"
    assert (await t.client.put(base, json={"visibility": "weird"})).status_code == 422
    assert (
        await t.client.put(base, json={"visibility": "selected", "user_ids": ["nope"]})
    ).status_code == 422
    assert (
        await t.client.put("/api/admin/tasks/missing/audience", json={"visibility": "all"})
    ).status_code == 404
    ok = await t.client.put(
        base, json={"visibility": "selected", "user_ids": [worker["id"]]}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["audience_user_ids"] == [worker["id"]]
    cleared = await t.client.put(base, json={"visibility": "all"})
    assert cleared.json()["audience_user_ids"] == []
    audits = [item for item in t.db.list_audit() if item["action"] == "task.audience.update"]
    assert len(audits) == 2


async def test_admin_user_task_access(task_app):
    t = task_app
    public = await create_task(t.client)
    restricted = await create_task(t.client, title="Закрытая")
    worker = t.users["worker"]
    await t.client.patch(
        f"/api/admin/tasks/{restricted['id']}",
        json={"visibility": "selected", "user_ids": []},
    )
    path = f"/api/admin/users/{worker['id']}/tasks"
    listing = await t.client.get(path)
    assert listing.status_code == 200, listing.text
    by_id = {item["id"]: item for item in listing.json()}
    assert by_id[public["id"]]["assigned"] is True
    assert by_id[restricted["id"]]["assigned"] is False
    granted = await t.client.put(
        f"/api/admin/users/{worker['id']}/task-access",
        json={"task_ids": [public["id"], restricted["id"]]},
    )
    assert granted.status_code == 200, granted.text
    assert {item["id"] for item in granted.json() if item["assigned"]} == {
        public["id"],
        restricted["id"],
    }
    await login(t.client, "worker")
    assert {item["id"] for item in (await t.client.get("/api/tasks")).json()} == {
        public["id"],
        restricted["id"],
    }
    await login(t.client)
    revoked = await t.client.put(
        f"/api/admin/users/{worker['id']}/task-access", json={"task_ids": []}
    )
    assert {item["id"] for item in revoked.json() if item["assigned"]} == {public["id"]}
    await login(t.client, "worker")
    assert {item["id"] for item in (await t.client.get("/api/tasks")).json()} == {
        public["id"]
    }
    assert (
        await t.client.get(f"/api/admin/users/{t.users['stranger']['id']}/tasks")
    ).status_code == 403
    await login(t.client)
    assert (await t.client.get("/api/admin/users/missing/tasks")).status_code == 404
    assert (
        await t.client.put(
            f"/api/admin/users/{worker['id']}/task-access", json={"task_ids": ["nope"]}
        )
    ).status_code == 422


async def test_task_run_title_uses_user_input(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    response = await t.client.post(
        f"/api/tasks/{task['id']}/runs",
        json={"user_input": "  Починить   пайплайн\nCI  "},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["title"] == f"{task['title']}: Починить пайплайн CI"
    job = t.db.get_job(run["id"])
    assert job["chat_title"] == "Починить пайплайн CI"
    assert job["task_initial_title"] == "Починить пайплайн CI"
    assert job["task_title"] == task["title"]
    assert job["content"] == "SECRET ADMIN INSTRUCTIONS\n\nПочинить   пайплайн\nCI"
    assert t.manager.client.created == []
    assert job["session_pending"] == 1
    assert (await t.client.get(f"/api/task-runs/{run['id']}")).json()["title"] == (
        f"{task['title']}: Починить пайплайн CI"
    )
    empty = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    assert empty["title"] == task["title"]


async def test_task_run_title_is_truncated_to_task_title_limit(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    run = (
        await t.client.post(
            f"/api/tasks/{task['id']}/runs", json={"user_input": "x" * 200}
        )
    ).json()
    assert run["title"] == f"{task['title']}: " + "x" * 120


async def test_admin_lists_and_syncs_user_task_runs(task_app):
    t = task_app
    task = await create_task(t.client)
    await login(t.client, "worker")
    worker_run = (await t.client.post(f"/api/tasks/{task['id']}/runs", json={})).json()
    await login(t.client, "stranger")
    stranger_run = (await t.client.post(
        f"/api/tasks/{task['id']}/runs", json={"user_input": "Stranger input"}
    )).json()
    worker = t.users["worker"]
    stranger = t.users["stranger"]
    worker_path = f"/api/admin/users/{worker['id']}/task-runs"
    assert (await t.client.get(worker_path)).status_code == 403
    assert (
        await t.client.post(f"{worker_path}/title-sync")
    ).status_code == 403
    await login(t.client)
    listing = await t.client.get(worker_path)
    assert listing.status_code == 200, listing.text
    runs = listing.json()
    assert [run["id"] for run in runs] == [worker_run["id"]]
    assert runs[0]["owner_login"] == "worker"
    assert (await t.client.get("/api/admin/users/missing/task-runs")).status_code == 404
    assert (
        await t.client.post("/api/admin/users/missing/task-runs/title-sync")
    ).status_code == 404
    audits = [item for item in t.db.list_audit() if item["action"] == "task_runs.admin_list"]
    assert audits and audits[0]["effective_user_id"] == worker["id"]
    await attach_run(t, worker_run)
    await attach_run(t, stranger_run)
    t.manager.client.generated_titles[t.db.get_job(worker_run["id"])["session_id"]] = (
        "Worker renamed"
    )
    t.manager.client.generated_titles[t.db.get_job(stranger_run["id"])["session_id"]] = (
        "Stranger renamed"
    )
    synced = await t.client.post(f"{worker_path}/title-sync")
    assert synced.status_code == 200, synced.text
    assert synced.json() == [{"id": worker_run["id"], "title": f"{task['title']}: Worker renamed"}]
    stranger_runs = (
        await t.client.get(f"/api/admin/users/{stranger['id']}/task-runs")
    ).json()
    assert stranger_runs[0]["title"] == f"{task['title']}: Stranger input"


async def test_admin_can_set_per_user_active_job_limit(task_app):
    t = task_app
    worker = t.users["worker"]
    path = f"/api/admin/users/{worker['id']}"
    await login(t.client, "worker")
    assert (
        await t.client.patch(path, json={"max_active_jobs": 2})
    ).status_code == 403
    await login(t.client)
    assert (await t.client.get("/api/admin/users")).json()[0]["max_active_jobs"] is None
    updated = await t.client.patch(path, json={"max_active_jobs": 2})
    assert updated.status_code == 200, updated.text
    assert updated.json()["max_active_jobs"] == 2
    listing = {
        user["id"]: user for user in (await t.client.get("/api/admin/users")).json()
    }
    assert listing[worker["id"]]["max_active_jobs"] == 2
    cleared = await t.client.patch(path, json={"max_active_jobs": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["max_active_jobs"] is None
    for bad in (0, -1, 6):
        assert (
            await t.client.patch(path, json={"max_active_jobs": bad})
        ).status_code == 422


async def test_user_and_admin_manage_verified_unique_mattermost_username(
    task_app, monkeypatch,
):
    t = task_app
    worker = t.users["worker"]
    stranger = t.users["stranger"]
    identities = {
        "akhamitsevich": {"id": "m" * 26, "username": "akhamitsevich"},
        "other.user": {"id": "n" * 26, "username": "other.user"},
    }

    async def resolve(_settings, username):
        return identities[username]

    monkeypatch.setattr("opencode_web.app.resolve_mattermost_username", resolve)
    await login(t.client, "worker")
    own = await t.client.patch(
        "/api/profile", json={"mattermost_username": "@Akhamitsevich"}
    )
    assert own.status_code == 200, own.text
    assert own.json()["mattermost_username"] == "akhamitsevich"
    assert own.json()["mattermost_user_id"] == "m" * 26
    assert t.db.get_user(worker["id"])["mattermost_user_id"] == "m" * 26
    assert (
        await t.client.patch(
            f"/api/admin/users/{stranger['id']}",
            json={"mattermost_username": "other.user"},
        )
    ).status_code == 403

    await login(t.client)
    path = f"/api/admin/users/{stranger['id']}"
    updated = await t.client.patch(path, json={"mattermost_username": "other.user"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["mattermost_username"] == "other.user"
    assert updated.json()["mattermost_user_id"] == "n" * 26
    duplicate = await t.client.patch(
        path, json={"mattermost_username": "akhamitsevich"}
    )
    assert duplicate.status_code == 409
    invalid = await t.client.patch(path, json={"mattermost_username": "12"})
    assert invalid.status_code == 422
    cleared = await t.client.patch(path, json={"mattermost_username": None})
    assert cleared.status_code == 200
    assert cleared.json()["mattermost_username"] is None
    assert cleared.json()["mattermost_user_id"] is None
    untouched = await t.client.patch(path, json={"status": "active"})
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["max_active_jobs"] is None


async def test_unknown_mattermost_username_is_not_saved(task_app, monkeypatch):
    async def missing(_settings, _username):
        raise MattermostNotificationError("http_404", permanent=True)

    monkeypatch.setattr("opencode_web.app.resolve_mattermost_username", missing)
    await login(task_app.client, "worker")
    response = await task_app.client.patch(
        "/api/profile", json={"mattermost_username": "missing.user"}
    )
    assert response.status_code == 422
    assert task_app.db.get_user(task_app.users["worker"]["id"])["mattermost_username"] is None


async def test_tasks_only_user_can_manage_mattermost_username(task_app, monkeypatch):
    t = task_app
    worker = t.users["worker"]
    t.db.update_user(worker["id"], tasks_only=True)

    async def resolve(_settings, username):
        return {"id": "m" * 26, "username": username}

    monkeypatch.setattr("opencode_web.app.resolve_mattermost_username", resolve)
    await login(t.client, "worker")

    updated = await t.client.patch(
        "/api/profile", json={"mattermost_username": "akhamitsevich"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["mattermost_username"] == "akhamitsevich"

    cleared = await t.client.patch(
        "/api/profile", json={"mattermost_username": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mattermost_username"] is None
