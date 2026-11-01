"""Exercise the MCP HTTP client against the real application and database."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from opencode_web.app import create_app
from opencode_web.config import Settings as ApplicationSettings
from opencode_web.database import Database

from config import Settings as MCPSettings
from valeria_client import ValeriaClient, ValeriaError


@pytest.fixture
async def task_api(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    password_file = tmp_path / "admin-password"
    password_file.write_text("correct-horse-battery-staple", encoding="utf-8")
    settings = ApplicationSettings(
        host="127.0.0.1", port=8080, base_url="http://test", database_path=tmp_path / "test.db",
        cookie_secure=False, max_active_sessions=1, max_active_sessions_hard_limit=5,
        max_active_sessions_per_user=0, max_active_sessions_per_user_hard_limit=5,
        max_queued_messages=100, allowed_workspace_roots=(workspace,),
        allow_shared_workspaces=False, opencode_bin=Path("/usr/bin/opencode"),
        opencode_host="127.0.0.1", opencode_port_start=45100, opencode_port_end=45120,
        opencode_username="opencode", opencode_password="test-opencode-password",
        default_agent="build", allowed_agents=("build", "plan"), default_model=None,
        allowed_models=("*",), opencode_start_timeout=2, session_secret="s" * 32,
        auth_session_ttl_hours=24, bootstrap_admin_login="admin",
        bootstrap_admin_password_file=password_file, log_level="INFO",
    )
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        ensure = AsyncMock(side_effect=AssertionError("Task API must not start OpenCode"))
        monkeypatch.setattr(app.state.manager, "ensure", ensure)
        users = {}
        for login in ("tasker", "stranger"):
            folder = workspace / login
            folder.mkdir()
            users[login] = db.create_user(
                login, "a-strong-password", "user", str(folder), tasks_only=True,
            )
        admin = db.get_user_by_login("admin")

        def make_task(title, *, published=True):
            return db.create_task(
                admin["id"], title, "Public description", "SECRET ADMIN INSTRUCTIONS", "build",
                "provider", "coder", "high", published,
            )

        task = make_task("Available task")
        draft = make_task("Draft task", published=False)
        private = make_task("Other user's task")
        db.set_task_audience(private["id"], "selected", [users["stranger"]["id"]])
        transport = httpx.ASGITransport(app=app)
        async with ValeriaClient(
            MCPSettings(url="http://test", login="tasker", password="a-strong-password"),
            transport=transport,
        ) as client:
            yield SimpleNamespace(
                client=client, db=db, task=task, draft=draft, private=private, users=users,
                transport=transport,
            )
        ensure.assert_not_awaited()


async def test_configured_user_can_launch_and_track_durable_queued_run(task_api):
    t = task_api
    catalog = await t.client.list_tasks()
    assert [task["id"] for task in catalog] == [t.task["id"]]
    assert "SECRET ADMIN INSTRUCTIONS" not in str(catalog)
    assert await t.client.list_task_runs() == []

    run = await t.client.create_task_run(
        t.task["id"], "Проверить сборку", idempotency_key="durable-launch",
    )
    assert run["status"] == "queued"
    assert run["task_id"] == t.task["id"]
    assert await t.client.list_task_runs() == [run]
    assert await t.client.get_task_run(run["id"]) == run
    assert await t.client.get_task_result(run["id"]) == {
        "run_id": run["id"], "status": "not_completed", "text": None,
    }

    # A separate SQLite connection must see the launch before any worker starts.
    reopened = Database(t.db.path, t.db.session_secret)
    try:
        reopened.initialize()
        job = reopened.get_job(run["id"])
        assert job["status"] == "queued"
        assert job["session_pending"] == 1
        assert job["actor_user_id"] == job["effective_user_id"] == t.users["tasker"]["id"]
        assert job["workspace_snapshot"] == t.users["tasker"]["workspace_path"]
        assert job["content"] == "SECRET ADMIN INSTRUCTIONS\n\nПроверить сборку"
        assert (job["agent"], job["provider_id"], job["model_id"], job["model_variant"]) == (
            "build", "provider", "coder", "high",
        )
    finally:
        reopened.close()

    t.db.set_job_status(run["id"], "running")
    assert (await t.client.get_task_run(run["id"]))["status"] == "running"
    t.db.complete_task_run(run["id"], {"text": "## Готово", "message_id": "final-answer"})
    assert (await t.client.get_task_run(run["id"]))["status"] == "completed"
    assert await t.client.get_task_result(run["id"]) == {
        "run_id": run["id"], "status": "available", "text": "## Готово",
    }
    assert "## Готово" not in str(await t.client.list_task_runs())


async def test_idempotency_replays_one_launch_and_rejects_changed_input(task_api):
    t = task_api
    run = await t.client.create_task_run(t.task["id"], "same", idempotency_key="retry-key")
    assert await t.client.create_task_run(
        t.task["id"], "same", idempotency_key="retry-key",
    ) == run
    with pytest.raises(ValeriaError) as caught:
        await t.client.create_task_run(t.task["id"], "changed", idempotency_key="retry-key")
    assert caught.value.status_code == 409
    assert caught.value.idempotency_key == "retry-key"
    assert [job["id"] for job in t.db.list_jobs()] == [run["id"]]


@pytest.mark.parametrize("hidden", ["draft", "private"])
async def test_invisible_task_cannot_be_launched(task_api, hidden):
    t = task_api
    task = getattr(t, hidden)
    assert task["id"] not in [item["id"] for item in await t.client.list_tasks()]
    with pytest.raises(ValeriaError) as caught:
        await t.client.create_task_run(task["id"], idempotency_key="hidden-task")
    assert caught.value.status_code == 404
    assert t.db.list_jobs() == []


async def test_run_status_and_result_are_limited_to_configured_user(task_api):
    t = task_api
    mine = await t.client.create_task_run(t.task["id"], "mine", idempotency_key="my-launch")
    async with ValeriaClient(
        MCPSettings(url="http://test", login="stranger", password="a-strong-password"),
        transport=t.transport,
    ) as other:
        theirs = await other.create_task_run(
            t.task["id"], "theirs", idempotency_key="their-launch",
        )
        t.db.complete_task_run(theirs["id"], {"text": "Private result"})
        assert [item["id"] for item in await other.list_task_runs()] == [theirs["id"]]
    assert [item["id"] for item in await t.client.list_task_runs()] == [mine["id"]]
    for read in (t.client.get_task_run, t.client.get_task_result):
        with pytest.raises(ValeriaError) as caught:
            await read(theirs["id"])
        assert caught.value.status_code == 404


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "completed"])
async def test_result_reports_pending_terminal_and_missing_answers(task_api, status):
    t = task_api
    run = await t.client.create_task_run(t.task["id"], idempotency_key="result-state")
    if status == "completed":
        t.db.complete_task_run(run["id"], None)
    else:
        t.db.set_job_status(run["id"], status)
    assert (await t.client.get_task_run(run["id"]))["status"] == status
    assert await t.client.get_task_result(run["id"]) == {
        "run_id": run["id"], "text": None,
        "status": "unavailable" if status == "completed" else "not_completed",
    }


async def test_saved_result_is_returned_without_reading_opencode_history(task_api):
    t = task_api
    run = await t.client.create_task_run(t.task["id"], idempotency_key="saved-result")
    t.db.set_job_status(run["id"], "completed")
    t.db.save_task_result(run["id"], {"text": "Persisted answer", "message_id": "saved"})
    assert await t.client.get_task_result(run["id"]) == {
        "run_id": run["id"], "status": "available", "text": "Persisted answer",
    }
