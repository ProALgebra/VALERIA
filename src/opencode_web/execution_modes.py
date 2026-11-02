"""Administrator-controlled global runtime changes with durable, separate histories."""

import asyncio
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from .config import ConfigError
from .database import Database, ExecutionModeChangingError
from .opencode_client import OpenCodeError
from .podman_runtime import PodmanError, PodmanRuntime
from .security import new_token


class ExecutionModeBody(BaseModel):
    mode: Literal["direct", "podman"]


def register_execution_modes(app, settings, database, admin_read_principal, admin_principal):
    change_lock = asyncio.Lock()

    async def availability():
        try:
            # A fresh check must notice removed images and broken runtime configuration.
            await PodmanRuntime(settings).preflight()
        except (PodmanError, OSError, TimeoutError) as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": True, "reason": None}

    async def status(db):
        return {
            "mode": db.execution_mode(),
            "availability": await availability(),
            "can_change": not db.has_unfinished_jobs() and not db._execution_mode_changing,
        }

    async def snapshot_histories(request, db):
        semaphore = asyncio.Semaphore(4)
        groups = {}
        for chat in db.mode_snapshot_chats():
            groups.setdefault(chat["workspace_snapshot"], []).append(chat)

        async def snapshot_workspace(workspace, chats):
            async with semaphore:
                for chat in chats:
                    try:
                        async with asyncio.timeout(30):
                            client = await request.app.state.manager.ensure(
                                workspace, expected_mode=chat["execution_mode"],
                            )
                            messages = await client.messages(chat["opencode_session_id"])
                        if not isinstance(messages, list):
                            raise OpenCodeError("Invalid session history")
                    except (OpenCodeError, ConfigError, TimeoutError) as exc:
                        raise HTTPException(
                            409,
                            "Не удалось сохранить историю диалогов. "
                            "Восстановите доступность текущего окружения и повторите смену режима",
                        ) from exc
                    db.save_chat_history(chat["id"], messages)

        # Finish every reader before allowing the transition or unfreezing submissions.
        results = await asyncio.gather(
            *(snapshot_workspace(workspace, chats) for workspace, chats in groups.items()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    @app.get("/api/admin/settings/execution-mode")
    async def get_execution_mode(
        principal: Annotated[dict, Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ):
        return await status(db)

    @app.put("/api/admin/settings/execution-mode")
    async def set_execution_mode(
        body: ExecutionModeBody, request: Request,
        principal: Annotated[dict, Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ):
        if change_lock.locked():
            raise HTTPException(409, "Режим работы уже меняется")
        async with change_lock:
            previous = db.execution_mode()
            if body.mode == previous:
                return await status(db)
            try:
                db.begin_execution_mode_change()
            except (ValueError, ExecutionModeChangingError) as exc:
                raise HTTPException(409, str(exc)) from exc
            manager = request.app.state.manager
            try:
                if body.mode == "podman":
                    await manager.podman.preflight(force=True)
                    for user in db.list_users():
                        workspace = user["workspace_path"]
                        if not workspace:
                            continue
                        if db.isolated_workspace_conflict(workspace, user["id"]):
                            raise HTTPException(
                                409, "Для изоляции назначьте пользователям отдельные, "
                                "не вложенные друг в друга рабочие папки",
                            )
                        await asyncio.to_thread(
                            manager.podman.build_launch,
                            settings.validate_workspace(workspace),
                            settings.opencode_port_start, settings.opencode_username, new_token(),
                        )
                await snapshot_histories(request, db)
                await manager.change_execution_mode(body.mode)
                try:
                    with db.transaction():
                        db.set_execution_mode(body.mode, principal["user"]["id"])
                        db.audit(
                            principal["user"]["id"], "execution_mode.update",
                            metadata={"previous": previous, "mode": body.mode},
                        )
                except BaseException:
                    await manager.change_execution_mode(previous)
                    raise
            except (PodmanError, ConfigError, OpenCodeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            finally:
                db.end_execution_mode_change()
        await request.app.state.events.publish(
            {"type": "execution_mode.updated", "mode": body.mode},
        )
        return await status(db)
