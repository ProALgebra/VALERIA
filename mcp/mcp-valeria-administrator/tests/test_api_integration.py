"""Exercise administrator tool requests against the real HTTP API and SQLite database."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from opencode_web.app import create_app
from opencode_web.config import Settings as ApplicationSettings

from admin_client import AdminClient, AdminError
from admin_config import Settings as MCPSettings
from admin_operations import OPERATIONS


@pytest.fixture
async def admin_api(tmp_path: Path, monkeypatch):
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
        ensure = AsyncMock(side_effect=AssertionError("Administrator API must not start OpenCode"))
        monkeypatch.setattr(app.state.manager, "ensure", ensure)
        admin = db.get_user_by_login("admin")
        admin_folder = workspace / "admin"
        admin_folder.mkdir()
        db.update_user(admin["id"], workspace_path=str(admin_folder))
        transport = httpx.ASGITransport(app=app)
        async with AdminClient(
            MCPSettings(url="http://test", login="admin", password="correct-horse-battery-staple"),
            transport=transport,
        ) as client:

            async def call(operation_name, **arguments):
                operation = OPERATIONS[f"valeria_admin_{operation_name}"]
                return await client.request(**operation.prepare(arguments))

            yield SimpleNamespace(
                call=call, client=client, db=db, workspace=workspace, admin=admin,
                transport=transport,
            )
        ensure.assert_not_awaited()


async def create_task(api, title="Task", **changes):
    return await api.call(
        "create_task", title=title, content="Private administrator instructions",
        agent="build", model="provider/coder", **changes,
    )


async def test_user_management_nullable_limits_password_reset_and_task_access(admin_api):
    a = admin_api
    folder = a.workspace / "operator"
    user = await a.call(
        "create_user", login="operator", password="initial-strong-password",
        workspace_path=str(folder), tasks_only=True,
    )
    assert user["login"] == "operator"
    assert user["role"] == "user"
    assert user["tasks_only"] is True
    assert folder.is_dir()
    assert "password" not in user
    assert {item["login"] for item in await a.call("list_users")} == {"admin", "operator"}

    updated = await a.call("update_user", user_id=user["id"], max_active_jobs=3)
    assert updated["max_active_jobs"] == 3
    updated = await a.call("update_user", user_id=user["id"], max_active_jobs=None)
    assert updated["max_active_jobs"] is None
    assert updated["tasks_only"] is True

    task = await create_task(a, visibility="selected", user_ids=[], is_published=True)
    access = await a.call("set_user_task_access", user_id=user["id"], task_ids=[task["id"]])
    assert next(item for item in access if item["id"] == task["id"])["assigned"] is True
    access = await a.call("list_user_tasks", user_id=user["id"])
    assert next(item for item in access if item["id"] == task["id"])["assigned"] is True
    access = await a.call("set_user_task_access", user_id=user["id"], task_ids=[])
    assert next(item for item in access if item["id"] == task["id"])["assigned"] is False
    assert await a.call("list_user_chats", user_id=user["id"]) == []
    assert await a.call("list_user_task_runs", user_id=user["id"]) == []

    assert await a.call(
        "reset_user_password", user_id=user["id"], password="replacement-strong-password",
    ) == {"ok": True}
    async with httpx.AsyncClient(transport=a.transport, base_url="http://test") as login_client:
        old = await login_client.post("/api/auth/login", json={
            "login": "operator", "password": "initial-strong-password",
        })
        assert old.status_code == 401
        new = await login_client.post("/api/auth/login", json={
            "login": "operator", "password": "replacement-strong-password",
        })
        assert new.status_code == 200

    await a.call("update_user", user_id=user["id"], status="disabled")
    assert a.db.get_user(user["id"])["status"] == "disabled"


async def test_task_chains_partial_updates_audience_and_deletion(admin_api):
    a = admin_api
    successor = await create_task(a, "Second task")
    first = await create_task(
        a, "First task", next_task_ids=[successor["id"]], description="Description",
        model_variant="high", is_published=True,
    )
    assert first["next_task_ids"] == [successor["id"]]
    assert first["model"] == "provider/coder"
    updated = await a.call(
        "update_task", task_id=first["id"], title="Renamed task", description=None,
        model_variant=None,
    )
    assert updated["title"] == "Renamed task"
    assert updated["description"] is None
    assert updated["model_variant"] is None
    assert updated["next_task_ids"] == [successor["id"]]
    assert bool(updated["is_published"]) is True
    updated = await a.call("update_task", task_id=first["id"], next_task_ids=[])
    assert updated["next_task_ids"] == []
    audience = await a.call(
        "set_task_audience", task_id=first["id"], visibility="selected", user_ids=[a.admin["id"]],
    )
    assert audience["visibility"] == "selected"
    assert audience["audience_user_ids"] == [a.admin["id"]]
    catalog = await a.call("list_tasks")
    assert {item["id"] for item in catalog} == {first["id"], successor["id"]}
    assert next(item for item in catalog if item["id"] == first["id"])["content"] == (
        "Private administrator instructions"
    )
    assert await a.call("delete_task", task_id=first["id"]) == {"ok": True}
    assert [item["id"] for item in await a.call("list_tasks")] == [successor["id"]]


async def test_queue_routing_concurrency_limits_and_archive_guards(admin_api):
    a = admin_api
    queue = await a.call("create_queue", name="Reports", priority=20, max_workers=2)
    queue_id = queue["id"]
    updated = await a.call(
        "update_queue", queue_id=queue_id, name="Reports fast", priority=30, max_workers=3,
    )
    assert (updated["name"], updated["priority"], updated["max_workers"]) == (
        "Reports fast", 30, 3,
    )
    assert queue_id in {item["id"] for item in await a.call("list_queues")}
    assert await a.call("set_default_chat_queue", queue_id=queue_id) == {"queue_id": queue_id}
    overview = await a.call("get_queue")
    assert overview["default_chat_queue_id"] == queue_id
    with pytest.raises(AdminError) as caught:
        await a.call("archive_queue", queue_id=queue_id)
    assert caught.value.status_code == 409

    assert await a.call("set_concurrency", value=2) == {"value": 2}
    assert (await a.call("get_queue"))["limit"] == 2
    with pytest.raises(AdminError) as caught:
        await a.call("set_concurrency", value=6)
    assert caught.value.status_code == 422
    assert await a.call("reset_concurrency") == {"ok": True}
    assert (await a.call("get_queue"))["limit"] == 1

    await a.call("set_default_chat_queue", queue_id="default")
    task = await create_task(a, queue_id=queue_id)
    with pytest.raises(AdminError) as caught:
        await a.call("archive_queue", queue_id=queue_id)
    assert caught.value.status_code == 409
    await a.call("delete_task", task_id=task["id"])
    assert await a.call("archive_queue", queue_id=queue_id) == {"ok": True}
    assert next(item for item in await a.call("list_queues") if item["id"] == queue_id)[
        "status"
    ] == "archived"
    with pytest.raises(AdminError) as caught:
        await a.call("archive_queue", queue_id="default")
    assert caught.value.status_code == 409


async def test_template_crud_preserves_omitted_fields_and_accepts_null_description(admin_api):
    a = admin_api
    template = await a.call(
        "create_template", title="Release checklist", description="Initial description",
        content="Check the release", is_published=True,
    )
    updated = await a.call(
        "update_template", template_id=template["id"], description=None, title="New checklist",
    )
    assert updated["title"] == "New checklist"
    assert updated["description"] is None
    assert updated["content"] == "Check the release"
    assert bool(updated["is_published"]) is True
    assert await a.call("list_templates") == [updated]
    assert await a.call("delete_template", template_id=template["id"]) == {"ok": True}
    assert await a.call("list_templates") == []


async def test_admin_launch_inspection_stop_results_statistics_and_audit(admin_api):
    a = admin_api
    task = await create_task(a, "Report", is_published=True)
    run = await a.call(
        "create_task_run", task_id=task["id"], user_input="November",
        idempotency_key="administrator-launch",
    )
    assert run["status"] == "queued"
    same = await a.call(
        "create_task_run", task_id=task["id"], user_input="November",
        idempotency_key="administrator-launch",
    )
    assert same == run
    jobs = await a.call("list_task_runs")
    assert jobs[0]["id"] == run["id"]
    assert jobs[0]["owner_login"] == "admin"
    assert (await a.call("get_queue"))["queued"] == 1
    job = await a.call("get_job", job_id=run["id"])
    assert job["effective_user_id"] == a.admin["id"]
    assert job["actor_user_id"] == a.admin["id"]
    assert job["content"] == "Private administrator instructions\n\nNovember"
    assert await a.call("get_task_result", run_id=run["id"]) == {
        "run_id": run["id"], "status": "not_completed", "text": None,
    }
    assert await a.call("get_task_run_messages", run_id=run["id"])
    stopped = await a.call("stop_job", job_id=run["id"])
    assert stopped["status"] == "cancelled"
    assert (await a.call("get_job", job_id=run["id"]))["status"] == "cancelled"

    completed = await a.call(
        "create_task_run", task_id=task["id"], idempotency_key="completed-launch",
    )
    a.db.complete_task_run(completed["id"], {"text": "Saved result", "message_id": "answer"})
    assert await a.call("get_task_result", run_id=completed["id"]) == {
        "run_id": completed["id"], "status": "available", "text": "Saved result",
    }
    statistics = await a.call("get_statistics", day="2026-11-01", timezone="Europe/Belgrade")
    assert statistics["day"] == "2026-11-01"
    assert statistics["timezone"] == "Europe/Belgrade"
    actions = {entry["action"] for entry in await a.call("get_audit")}
    assert {"task.create", "job.stop.request", "job.stop.completed"} <= actions


async def test_consultation_settings_keep_queue_when_omitted_and_protect_used_queue(admin_api):
    a = admin_api
    queue = await a.call("create_queue", name="Consultations", priority=10, max_workers=1)
    saved = await a.call(
        "set_consultation_settings", model="provider/coder", queue_id=queue["id"],
    )
    assert saved == {"model": "provider/coder", "queue_id": queue["id"]}
    assert await a.call("get_consultation_settings") == saved
    saved = await a.call("set_consultation_settings", model="provider/other")
    assert saved["queue_id"] == queue["id"]
    assert saved["model"] == "provider/other"
    with pytest.raises(AdminError) as caught:
        await a.call("archive_queue", queue_id=queue["id"])
    assert caught.value.status_code == 409
    assert await a.call("list_consultations") == []
    assert await a.call("get_mcp_status") == []
