from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from opencode_web.app import create_app
from opencode_web.database import Database


class TitleClient:
    def __init__(self, session_id: str, title: str) -> None:
        self.session_id = session_id
        self.title = title

    async def sessions(self) -> list[dict[str, Any]]:
        return [{"id": self.session_id, "title": self.title}]


class TitleManager:
    def __init__(self, client: TitleClient) -> None:
        self.client = client
        self.ensure_calls = 0

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True):
        assert wake is False
        assert activity is False
        yield self.client

    async def ensure(self, workspace: str) -> TitleClient:
        self.ensure_calls += 1
        return self.client


def endpoint(app: FastAPI, path: str, method: str) -> Callable[..., Any]:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"Route not found: {method} {path}")


async def test_cached_chat_list_does_not_wait_for_title_sync(settings, workspace) -> None:
    user_workspace = workspace / "project"
    user_workspace.mkdir()
    database = Database(settings.database_path, settings.session_secret)
    database.initialize()
    user = database.create_user(
        "worker", "long-enough-password", "user", str(user_workspace)
    )
    chat = database.create_chat(
        user["id"], "session-1", "Cached title", str(user_workspace)
    )

    app = create_app(settings, start_dispatcher=False)
    manager = TitleManager(TitleClient("session-1", "OpenCode title"))
    app.state.manager = manager

    principal = {"user": user}
    list_chats = endpoint(app, "/api/chats", "GET")
    title_sync = endpoint(app, "/api/chats/title-sync", "POST")
    request = Request({"type": "http", "app": app, "headers": []})

    cached = await list_chats(principal=principal, db=database)
    assert cached[0]["title"] == "Cached title"
    assert manager.ensure_calls == 0

    synced = await title_sync(request=request, principal=principal, db=database)
    assert synced == [{"id": chat["id"], "title": "OpenCode title"}]
    assert manager.ensure_calls == 0

    unchanged = await title_sync(request=request, principal=principal, db=database)
    assert unchanged == []

    database.close()


async def test_admin_title_sync_only_reads_live_workspaces(settings, workspace, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from opencode_web.opencode_client import OpenCodeInstance, OpenCodeManager

    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    manager = OpenCodeManager(settings)
    monkeypatch.setattr(
        manager, "_ensure_client", AsyncMock(side_effect=AssertionError("Must not wake OpenCode"))
    )
    try:
        admin = db.create_user("admin", "long-admin-password", "admin", None)
        task = db.create_task(
            admin["id"], "Check CI", "Description", "Instructions", "build",
            "test-provider", "test-model", None, True,
        )
        jobs = []
        for name in ["active", "sleeping", "historical"]:
            path = workspace / name
            if name != "historical":
                path.mkdir()
            user = db.create_user(name, "long-user-password", "user", str(path))
            job = db.create_task_job(
                task=task, user=user, actor_user_id=user["id"], session_id=f"ses-{name}",
                user_input=name, max_queued=10, session_title=f"Saved {name}",
            )
            db.set_job_status(job["id"], "completed")
            jobs.append(job)
        client = SimpleNamespace(sessions=AsyncMock(return_value=[
            {"id": "ses-active", "title": "Updated active"}
        ]))
        instance = OpenCodeInstance(
            workspace / "active", 45100, SimpleNamespace(returncode=None), client,
        )
        instance.ready.set()
        instance.idle_since = 100.0
        instance.activity_version = 7
        manager._instances[str(instance.workspace)] = instance
        app = create_app(settings, start_dispatcher=False)
        app.state.manager = manager
        request = Request({"type": "http", "app": app, "headers": []})
        sync = endpoint(app, "/api/admin/task-runs/title-sync", "POST")

        changed = await sync(request=request, principal={"user": admin}, db=db)
        assert changed == [{"id": jobs[0]["id"], "title": "Check CI: Updated active"}]
        assert db.get_chat(jobs[1]["chat_id"])["title"] == "Saved sleeping"
        assert db.get_chat(jobs[2]["chat_id"])["title"] == "Saved historical"
        assert manager.instances() == [instance]
        assert instance.idle_since == 100.0
        assert instance.activity_version == 7
        assert instance.users == 0
        manager._ensure_client.assert_not_awaited()

        # Repeated background events, including after all processes have stopped.
        for _ in range(2):
            assert await sync(request=request, principal={"user": admin}, db=db) == []
        manager._instances.clear()
        assert await sync(request=request, principal={"user": admin}, db=db) == []
        assert manager.instances() == []
        manager._ensure_client.assert_not_awaited()
    finally:
        manager._instances.clear()
        await manager.close()
        db.close()
