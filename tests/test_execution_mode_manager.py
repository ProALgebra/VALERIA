"""Exercise runtime transitions at the manager boundary without launching processes."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencode_web.opencode_client import OpenCodeError, OpenCodeManager
from opencode_web.podman_runtime import PodmanError, PodmanProcess


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)
        if self.returncode is None:
            self.returncode = -int(sig)

    async def wait(self):
        assert self.returncode is not None, "Process must be stopped before it can be reaped"
        return self.returncode


class FakeClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.close = AsyncMock()
        self.session = AsyncMock(side_effect=lambda session_id: {"id": session_id})


@pytest.fixture
async def runtime_factory(settings, workspace, tmp_path, monkeypatch):
    processes = []
    managers = []

    def process():
        result = FakeProcess(900_000 + len(processes))
        processes.append(result)
        return result

    direct_spawn = AsyncMock(side_effect=lambda *args, **kwargs: process())
    monkeypatch.setattr("opencode_web.opencode_client.OpenCodeClient", FakeClient)
    monkeypatch.setattr("opencode_web.opencode_client.asyncio.create_subprocess_exec", direct_spawn)
    # Fake process IDs must never reach the host's real process-group signaling.
    monkeypatch.setattr(
        OpenCodeManager, "_signal_instance",
        staticmethod(lambda instance, sig: instance.process.send_signal(sig)),
    )

    def factory(mode="direct"):
        config = replace(
            settings, opencode_bin=Path(sys.executable),
            podman_state_root=tmp_path / "private-runtime-state",
        )
        manager = OpenCodeManager(config, execution_mode=mode)
        managers.append(manager)
        monkeypatch.setattr(manager, "_free_port", lambda: 45100 + len(manager._instances))

        async def ready(instance):
            instance.ready.set()
            return instance.client

        async def start(workspace, port, username, password):
            return PodmanProcess(process(), f"isolated-{workspace.name}-{port}")

        monkeypatch.setattr(manager, "_start_instance", AsyncMock(side_effect=ready))
        monkeypatch.setattr(manager.podman, "preflight", AsyncMock())
        monkeypatch.setattr(manager.podman, "reconcile", AsyncMock())
        monkeypatch.setattr(manager.podman, "start", AsyncMock(side_effect=start))
        monkeypatch.setattr(manager.podman, "stop", AsyncMock())
        return SimpleNamespace(
            manager=manager, podman=manager.podman, direct_spawn=direct_spawn,
            processes=processes, startup=manager._start_instance, workspace=workspace,
        )

    yield factory
    for manager in managers:
        manager.podman.stop.side_effect = None
        manager.podman.reconcile.side_effect = None
        await manager.close()


async def test_direct_default_keeps_process_lifecycle_and_restartable_handles(runtime_factory):
    runtime = runtime_factory()
    manager, workspace = runtime.manager, runtime.workspace
    handle = await manager.ensure(str(workspace))
    assert manager.execution_mode == handle.execution_mode == "direct"
    assert await manager.ensure(str(workspace)) is handle
    assert runtime.direct_spawn.await_count == 1
    args, kwargs = runtime.direct_spawn.call_args
    assert args[:2] == (str(manager.settings.opencode_bin), "serve")
    assert kwargs["cwd"] == workspace
    assert kwargs["env"]["OPENCODE_SERVER_USERNAME"] == manager.settings.opencode_username
    assert kwargs["env"]["OPENCODE_SERVER_PASSWORD"] == manager.settings.opencode_password
    assert kwargs["start_new_session"] == (os.name == "posix")
    old = manager.instances()[0]

    await manager.stop_workspace(str(workspace))
    old.client.close.assert_awaited_once()
    assert old.process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert manager.instances() == []
    assert await handle.session("saved-session") == {"id": "saved-session"}
    assert runtime.direct_spawn.await_count == 2
    assert manager.instances()[0].process is not old.process
    runtime.podman.preflight.assert_not_awaited()
    runtime.podman.reconcile.assert_not_awaited()
    runtime.podman.start.assert_not_awaited()


async def test_podman_workspaces_receive_distinct_credentials_and_local_clients(runtime_factory):
    runtime = runtime_factory("podman")
    workspaces = [runtime.workspace / name for name in ("first", "second")]
    for workspace in workspaces:
        workspace.mkdir()
        await runtime.manager.ensure(str(workspace))
    clients = [instance.client for instance in runtime.manager.instances()]
    assert len({client.password for client in clients}) == 2
    assert all(client.password != runtime.manager.settings.opencode_password for client in clients)
    assert all(client.base_url.startswith("http://127.0.0.1:") for client in clients)
    for call, workspace, client in zip(
        runtime.podman.start.call_args_list, workspaces, clients, strict=True,
    ):
        assert call.args == (workspace, int(client.base_url.rsplit(":", 1)[1]),
                             runtime.manager.settings.opencode_username, client.password)
    runtime.podman.preflight.assert_awaited_once()
    runtime.podman.reconcile.assert_awaited_once()
    runtime.direct_spawn.assert_not_awaited()


@pytest.mark.parametrize("failure", ["preflight", "reconcile", "start"])
async def test_podman_failure_never_launches_direct_process(runtime_factory, failure):
    runtime = runtime_factory("podman")
    failing_call = getattr(runtime.podman, failure)
    original = failing_call.side_effect
    failing_call.side_effect = PodmanError(f"{failure} failed")
    with pytest.raises(OpenCodeError, match=f"{failure} failed"):
        await runtime.manager.ensure(str(runtime.workspace))
    assert runtime.manager.execution_mode == "podman"
    assert runtime.manager.instances() == []
    runtime.direct_spawn.assert_not_awaited()
    failing_call.side_effect = original
    await runtime.manager.ensure(str(runtime.workspace))
    assert len(runtime.manager.instances()) == 1
    runtime.direct_spawn.assert_not_awaited()


@pytest.mark.parametrize("old_mode,new_mode", [("direct", "podman"), ("podman", "direct")])
async def test_old_handle_and_expected_mode_cannot_launch_in_new_runtime(
    runtime_factory, old_mode, new_mode,
):
    runtime = runtime_factory(old_mode)
    manager, workspace = runtime.manager, str(runtime.workspace)
    old_handle = await manager.ensure(workspace)
    await manager.change_execution_mode(new_mode)
    direct_before = runtime.direct_spawn.await_count
    podman_before = runtime.podman.start.await_count
    with pytest.raises(OpenCodeError):
        await old_handle.session("old-session")
    with pytest.raises(OpenCodeError):
        await manager.ensure(workspace, expected_mode=old_mode)
    with pytest.raises(OpenCodeError):
        async with manager.use(workspace, wake=False, expected_mode=old_mode):
            pytest.fail("An obsolete runtime lease must never be yielded")
    assert runtime.direct_spawn.await_count == direct_before
    assert runtime.podman.start.await_count == podman_before
    assert manager.instances() == []
    current = await manager.ensure(workspace, expected_mode=new_mode)
    assert current.execution_mode == new_mode
    assert await current.session("new-session") == {"id": "new-session"}


async def test_returning_to_original_mode_allows_original_history_handle(runtime_factory):
    runtime = runtime_factory()
    manager, workspace = runtime.manager, str(runtime.workspace)
    handle = await manager.ensure(workspace)
    await manager.change_execution_mode("podman")
    await manager.change_execution_mode("direct")
    assert await handle.session("original-session") == {"id": "original-session"}
    assert manager.execution_mode == handle.execution_mode == "direct"
    assert runtime.direct_spawn.await_count == 2
    runtime.podman.start.assert_not_awaited()


async def test_active_lease_blocks_mode_change_without_stopping_process(runtime_factory):
    runtime = runtime_factory()
    manager, workspace = runtime.manager, str(runtime.workspace)
    await manager.ensure(workspace)
    instance = manager.instances()[0]
    async with manager.use(workspace, expected_mode="direct"):
        with pytest.raises(OpenCodeError):
            await manager.change_execution_mode("podman")
        assert manager.execution_mode == "direct"
        assert instance.users == 1
        assert instance.process.returncode is None
        instance.client.close.assert_not_awaited()
        runtime.podman.reconcile.assert_not_awaited()
    assert instance.users == 0
    await manager.change_execution_mode("podman")
    assert manager.execution_mode == "podman"
    assert manager.instances() == []


async def test_unready_startup_blocks_mode_change(runtime_factory):
    runtime = runtime_factory()
    manager = runtime.manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(instance):
        entered.set()
        await release.wait()
        instance.ready.set()
        return instance.client

    runtime.startup.side_effect = delayed
    task = asyncio.create_task(manager.ensure(str(runtime.workspace)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(OpenCodeError):
            await manager.change_execution_mode("podman")
        assert manager.execution_mode == "direct"
        assert runtime.processes[0].returncode is None
        release.set()
        await asyncio.wait_for(task, 1)
        await manager.change_execution_mode("podman")
        assert manager.instances() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_container_cleanup_keeps_mode_and_blocks_replacement(runtime_factory):
    runtime = runtime_factory("podman")
    manager, workspace = runtime.manager, str(runtime.workspace)
    await manager.ensure(workspace)
    instance = manager.instances()[0]
    runtime.podman.stop.side_effect = PodmanError("container removal failed")
    with pytest.raises(OpenCodeError, match="container removal failed"):
        await manager.change_execution_mode("direct")
    assert manager.execution_mode == "podman"
    assert not manager._switching_mode
    assert manager._instances[workspace] is instance
    assert instance.cleanup_pending
    with pytest.raises(OpenCodeError, match="container removal failed"):
        await manager.ensure(workspace)
    assert runtime.podman.start.await_count == 1
    runtime.direct_spawn.assert_not_awaited()


async def test_idle_cleanup_failure_keeps_container_owned_until_retry(runtime_factory, monkeypatch):
    runtime = runtime_factory("podman")
    manager, workspace = runtime.manager, str(runtime.workspace)
    await manager.ensure(workspace)
    instance = manager.instances()[0]
    instance.client._request = AsyncMock(side_effect=lambda method, path: (
        {} if path == "/session/status" else []
    ))
    manager._has_pending_jobs = lambda _: False
    clock = [100.0]
    monkeypatch.setattr("opencode_web.opencode_client.monotonic", lambda: clock[0])
    await manager.reap_idle()
    runtime.podman.stop.side_effect = PodmanError("container removal failed")
    clock[0] += manager.settings.opencode_idle_timeout_seconds
    await manager.reap_idle()
    assert manager._instances[workspace] is instance
    assert instance.cleanup_pending
    with pytest.raises(OpenCodeError, match="container removal failed"):
        await manager.ensure(workspace)
    assert runtime.podman.start.await_count == 1
    runtime.podman.stop.side_effect = None
    await manager.ensure(workspace)
    assert runtime.podman.start.await_count == 2
    assert manager.instances()[0] is not instance


@pytest.mark.parametrize("old_mode,new_mode", [("podman", "direct"), ("direct", "podman")])
async def test_empty_manager_reconciles_persisted_containers_before_mode_change(
    runtime_factory, old_mode, new_mode,
):
    runtime = runtime_factory(old_mode)
    assert runtime.manager.instances() == []
    await runtime.manager.change_execution_mode(new_mode)
    runtime.podman.reconcile.assert_awaited_once()
    assert runtime.manager.execution_mode == new_mode
    runtime.podman.start.assert_not_awaited()
    runtime.direct_spawn.assert_not_awaited()


async def test_orphan_cleanup_failure_does_not_commit_new_mode(runtime_factory):
    runtime = runtime_factory("podman")
    runtime.podman.reconcile.side_effect = PodmanError("orphan removal failed")
    with pytest.raises(OpenCodeError, match="orphan removal failed"):
        await runtime.manager.change_execution_mode("direct")
    assert runtime.manager.execution_mode == "podman"
    assert not runtime.manager._switching_mode
    assert runtime.manager.instances() == []
    runtime.direct_spawn.assert_not_awaited()


@pytest.mark.parametrize("mode", ["direct", "podman"])
async def test_cancelled_startup_reaps_process_and_container(runtime_factory, mode):
    runtime = runtime_factory(mode)
    manager = runtime.manager
    entered = asyncio.Event()

    async def stalled(instance):
        entered.set()
        await asyncio.Event().wait()

    runtime.startup.side_effect = stalled
    task = asyncio.create_task(manager.ensure(str(runtime.workspace)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        instance = manager.instances()[0]
        container_name = instance.container_name
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.instances() == []
        assert instance.users == 0
        assert instance.process.returncode is not None
        instance.client.close.assert_awaited_once()
        assert instance.process.signals == [signal.SIGTERM, signal.SIGKILL]
        if mode == "podman":
            runtime.podman.stop.assert_awaited_once_with(container_name)
            assert instance.container_name is None
            runtime.direct_spawn.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_orphan_cleanup_keeps_previous_mode_and_allows_retry(runtime_factory):
    runtime = runtime_factory("podman")
    entered = asyncio.Event()

    async def stalled_cleanup():
        entered.set()
        await asyncio.Event().wait()

    runtime.podman.reconcile.side_effect = stalled_cleanup
    task = asyncio.create_task(runtime.manager.change_execution_mode("direct"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.manager.execution_mode == "podman"
        assert not runtime.manager._switching_mode
        runtime.podman.reconcile.side_effect = None
        await runtime.manager.change_execution_mode("direct")
        assert runtime.manager.execution_mode == "direct"
        runtime.direct_spawn.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
