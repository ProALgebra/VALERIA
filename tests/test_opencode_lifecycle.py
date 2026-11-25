from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opencode_web.opencode_client import OpenCodeError, OpenCodeManager

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process groups")


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


async def wait_dead(pid: int) -> None:
    async with asyncio.timeout(5):
        while alive(pid):
            await asyncio.sleep(0.02)


@pytest.fixture
def process_manager(settings, tmp_path):
    executable = tmp_path / "fake-opencode"
    payload = tmp_path / "fake-opencode.py"
    # The kernel cannot parse a shebang whose interpreter path contains spaces.
    # A tiny POSIX wrapper keeps the fixture independent of the project path.
    payload.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-u', '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(1, flush=True); time.sleep(300)'], stdout=subprocess.PIPE)\n"
        "child.stdout.readline()\n"
        "Path('processes.json').write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(300)\n"
    )
    executable.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(payload))}\n"
    )
    executable.chmod(0o700)
    manager = OpenCodeManager(replace(settings, opencode_bin=executable))
    return manager


async def wait_started(instance):
    marker = instance.workspace / "processes.json"
    async with asyncio.timeout(5):
        while True:
            try:
                pids = json.loads(marker.read_text())
                if pids[0] == instance.process.pid:
                    instance.ready.set()
                    return instance.client
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            await asyncio.sleep(0.02)


async def test_stop_kills_children_even_after_parent_exits(process_manager, workspace, monkeypatch):
    manager = process_manager
    monkeypatch.setattr(manager, "_start_instance", wait_started)
    try:
        await manager.ensure(str(workspace))
        parent, child = json.loads((workspace / "processes.json").read_text())
        assert os.getpgid(parent) == parent
        assert os.getpgid(child) == parent
        assert os.getpgid(parent) != os.getpgrp()
        await manager.stop_workspace(str(workspace))
        await wait_dead(parent)
        await wait_dead(child)
        assert manager.instances() == []
    finally:
        await manager.close()


async def test_replacement_cleans_children_of_crashed_instance(
    process_manager, workspace, monkeypatch
):
    manager = process_manager
    monkeypatch.setattr(manager, "_start_instance", wait_started)
    try:
        await manager.ensure(str(workspace))
        old = manager.instances()[0]
        _, old_child = json.loads((workspace / "processes.json").read_text())
        old.process.kill()
        await old.process.wait()
        assert alive(old_child)
        await manager.ensure(str(workspace))
        await wait_dead(old_child)
        assert manager.instances()[0].process.pid != old.process.pid
        assert len(manager.instances()) == 1
    finally:
        await manager.close()


async def test_client_cleanup_error_still_kills_process_tree(
    process_manager, workspace, monkeypatch
):
    manager = process_manager
    monkeypatch.setattr(manager, "_start_instance", wait_started)
    try:
        await manager.ensure(str(workspace))
        instance = manager.instances()[0]
        parent, child = json.loads((workspace / "processes.json").read_text())
        # Close the real HTTP client first so the injected failure cannot leak it.
        await instance.client.close()
        monkeypatch.setattr(instance.client, "close", AsyncMock(side_effect=RuntimeError("close")))
        with pytest.raises(RuntimeError, match="close"):
            await manager.stop_workspace(str(workspace))
        await wait_dead(parent)
        await wait_dead(child)
    finally:
        await manager.close()


async def test_cancelled_startup_releases_process_tree(process_manager, workspace, monkeypatch):
    manager = process_manager
    started = asyncio.Event()

    async def stalled_start(instance):
        await wait_started(instance)
        instance.ready.clear()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_start_instance", stalled_start)
    task = asyncio.create_task(manager.ensure(str(workspace)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        parent, child = json.loads((workspace / "processes.json").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_dead(parent)
        await wait_dead(child)
        assert manager.instances() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_close_rejects_new_processes(process_manager, workspace):
    await process_manager.close()
    with pytest.raises(OpenCodeError, match="shutting down"):
        await process_manager.ensure(str(workspace))


async def test_idle_stop_kills_tree_and_old_client_restarts_workspace(
    process_manager, workspace, monkeypatch
):
    manager = process_manager
    monkeypatch.setattr(manager, "_start_instance", wait_started)
    manager._has_pending_jobs = lambda _: False
    clock = [100.0]
    monkeypatch.setattr("opencode_web.opencode_client.monotonic", lambda: clock[0])
    from opencode_web.opencode_client import OpenCodeClient

    async def request(self, method, path, **kwargs):
        if path == "/session/status":
            return {}
        if path in {"/permission", "/question"}:
            return []
        if path == "/session/saved-session":
            return {"id": "saved-session", "title": "Existing chat"}
        raise AssertionError(path)

    monkeypatch.setattr(OpenCodeClient, "_request", request)
    try:
        client = await manager.ensure(str(workspace))
        parent, child = json.loads((workspace / "processes.json").read_text())
        await manager.reap_idle()
        clock[0] += manager.settings.opencode_idle_timeout_seconds
        await manager.reap_idle()
        await wait_dead(parent)
        await wait_dead(child)
        assert manager.instances() == []
        # Caller retained its handle across shutdown; it must resolve a fresh process.
        session = await client.session("saved-session")
        assert session["id"] == "saved-session"
        assert manager.instances()[0].process.pid != parent
    finally:
        await manager.close()
