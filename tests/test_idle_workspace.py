from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencode_web.database import Database
from opencode_web.opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeInstance,
    OpenCodeManager,
)


@pytest.fixture
async def idle_manager(settings, workspace, monkeypatch):
    manager = OpenCodeManager(replace(settings, opencode_idle_timeout_seconds=15))
    response = {"/session/status": {}, "/permission": [], "/question": []}

    async def request(method, path, **kwargs):
        return response[path]

    client = OpenCodeClient("http://test", "", "")
    client._request = AsyncMock(side_effect=request)
    instance = OpenCodeInstance(workspace, 45100, SimpleNamespace(returncode=None), client)
    instance.ready.set()
    manager._instances[str(workspace)] = instance
    manager._has_pending_jobs = lambda _: False
    monkeypatch.setattr(manager, "_stop_instance", AsyncMock())
    clock = [100.0]
    monkeypatch.setattr("opencode_web.opencode_client.monotonic", lambda: clock[0])
    yield manager, instance, response, clock
    await client.close()


async def test_idle_stop_starts_countdown_only_after_confirmed_idle(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    assert instance.idle_since == 100
    clock[0] += 14
    await manager.reap_idle()
    assert manager.instances() == [instance]
    clock[0] += 1
    await manager.reap_idle()
    assert manager.instances() == []
    manager._stop_instance.assert_awaited_once_with(instance)


@pytest.mark.parametrize("blocker", [
    {"/session/status": {"s1": {"type": "busy"}}},
    {"/session/status": {"s1": {"type": "idle"}, "s2": {"type": "retry"}}},
    {"/permission": [{"sessionID": "s2"}]},
    {"/question": [{"sessionID": "s2"}]},
    {"/session/status": []},
    {"/session/status": {"s1": {"type": "unknown"}}},
    {"/session/status": {"s1": None}},
    {"/permission": {}},
    {"/question": None},
])
async def test_busy_waiting_and_uncertain_states_never_stop(idle_manager, blocker):
    manager, instance, response, clock = idle_manager
    await manager.reap_idle()
    response.update(blocker)
    clock[0] += 100000
    await manager.reap_idle()
    assert manager.instances() == [instance]
    assert instance.idle_since is None
    manager._stop_instance.assert_not_called()


async def test_errors_restart_full_idle_countdown(idle_manager):
    manager, instance, response, clock = idle_manager
    await manager.reap_idle()
    original = instance.client._request.side_effect
    instance.client._request.side_effect = OpenCodeError("offline")
    clock[0] += 100000
    await manager.reap_idle()
    assert instance.idle_since is None
    instance.client._request.side_effect = original
    await manager.reap_idle()
    assert instance.idle_since == clock[0]
    manager._stop_instance.assert_not_called()


async def test_queued_and_active_jobs_in_same_workspace_block_stop(
    idle_manager, settings, workspace
):
    manager, instance, _, clock = idle_manager
    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    try:
        user = db.create_user("idle-user", "test-password-long", "user", str(workspace))
        chat = db.create_chat(user["id"], "ses1", "Test", str(workspace))
        jobs = [db.create_job(chat=chat, effective_user_id=user["id"], actor_user_id=user["id"],
                              content="hello", agent="build", provider_id=None, model_id=None,
                              model_variant=None)
                for _ in range(2)]
        manager._has_pending_jobs = db.workspace_has_pending_jobs
        for state in [
            "queued", "dispatching", "running", "waiting_permission",
            "waiting_question", "cancelling",
        ]:
            db.set_job_status(jobs[0]["id"], state)
            clock[0] += 100000
            await manager.reap_idle()
            assert instance.idle_since is None
        db.set_job_status(jobs[0]["id"], "cancelled")
        await manager.reap_idle()
        assert instance.idle_since is None  # second message still queued on same session
        db.set_job_status(jobs[1]["id"], "completed")
        await manager.reap_idle()
        clock[0] += 15
        await manager.reap_idle()
        assert manager.instances() == []
    finally:
        db.close()


async def test_leases_protect_inflight_requests(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    async with manager.use(str(instance.workspace)):
        clock[0] += 100000
        await manager.reap_idle()
        assert manager.instances() == [instance]
    assert instance.users == 0
    await manager.reap_idle()
    assert instance.idle_since == clock[0]


async def test_polling_does_not_refresh_or_wake(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    async with manager.use(str(instance.workspace), wake=False, activity=False) as client:
        assert client is instance.client
    assert instance.idle_since == 100
    clock[0] += 15
    await manager.reap_idle()
    async with manager.use(str(instance.workspace), wake=False, activity=False) as client:
        assert client is None
    assert manager.instances() == []


async def test_new_request_during_idle_inspection_prevents_stop(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    clock[0] += 15
    entered, release = asyncio.Event(), asyncio.Event()
    original = instance.client._request.side_effect

    async def delayed(method, path, **kwargs):
        entered.set()
        await release.wait()
        return await original(method, path, **kwargs)

    instance.client._request.side_effect = delayed
    reap = asyncio.create_task(manager.reap_idle())
    await entered.wait()
    async with manager.use(str(instance.workspace)):
        pass
    release.set()
    await reap
    assert manager.instances() == [instance]
    assert instance.idle_since is None


async def test_job_queued_during_inspection_prevents_stop(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    clock[0] += 15
    original = instance.client._request.side_effect

    async def enqueue(method, path, **kwargs):
        manager._has_pending_jobs = lambda _: True
        return await original(method, path, **kwargs)

    instance.client._request.side_effect = enqueue
    await manager.reap_idle()
    assert manager.instances() == [instance]
    assert instance.idle_since is None


async def test_disabled_and_unconfigured_reaper_never_stop(idle_manager):
    manager, instance, _, clock = idle_manager
    instance.idle_since = 0
    manager.settings = replace(manager.settings, opencode_idle_timeout_seconds=0)
    await manager.reap_idle()
    manager.start_idle_reaper(lambda _: False)
    assert manager._idle_task is None
    assert manager.instances() == [instance]


async def test_cancelled_lease_is_released(idle_manager):
    manager, instance, _, _ = idle_manager
    started = asyncio.Event()

    async def work():
        async with manager.use(str(instance.workspace)):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instance.users == 0


async def test_background_request_inflight_does_not_reset_countdown(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    clock[0] += 15
    async with manager.use(str(instance.workspace), wake=False, activity=False):
        await manager.reap_idle()
        assert manager.instances() == [instance]
        assert instance.idle_since == 100
    await manager.reap_idle()
    assert manager.instances() == []


async def test_pending_check_failure_keeps_process(idle_manager):
    manager, instance, _, clock = idle_manager
    await manager.reap_idle()
    clock[0] += 15

    def broken_db(_):
        raise RuntimeError("database unavailable")

    manager._has_pending_jobs = broken_db
    await manager.reap_idle()
    assert manager.instances() == [instance]
    assert instance.idle_since is None


async def test_reaper_lifecycle(settings):
    manager = OpenCodeManager(settings)
    manager.start_idle_reaper(lambda _: False)
    task = manager._idle_task
    await manager.close()
    assert task.done()
    assert manager._idle_task is None


def test_background_routes_do_not_start_stopped_workspace(settings, workspace):
    from fastapi.testclient import TestClient
    from test_api import login

    from opencode_web.app import create_app

    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        db = app.state.database
        user = db.create_user("idle-user", "test-password-long", "user", str(workspace))
        chat = db.create_chat(user["id"], "saved-session", "Saved", str(workspace))
        login(client, "idle-user", "test-password-long")
        for endpoint in ["questions", "permissions"]:
            response = client.get(f"/api/chats/{chat['id']}/{endpoint}")
            assert response.status_code == 200, response.text
            assert response.json() == []
        assert app.state.manager.instances() == []
