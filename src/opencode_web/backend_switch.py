"""Offline backend transitions preserve histories before changing server versions."""

import socket
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, Settings
from .database import Database
from .opencode_client import OpenCodeManager


@contextmanager
def database_process_lock(path: Path):
    # Both the service and preparation command hold this for their whole lifetime.
    import fcntl

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".runtime.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError(
                "Stop the OpenCode Web service before preparing a backend switch"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def prepare_backend_switch(settings: Settings, target: str) -> int:
    """Called with the OLD settings and binary, while the service is stopped."""
    with database_process_lock(settings.database_path):
        database = Database(settings.database_path, settings.session_secret)
        database.initialize()
        try:
            if database.backend_version() != settings.opencode_api_version:
                raise ConfigError("Use the current backend configuration to prepare the switch")
            if target == database.backend_version() or target not in {"v1", "v2", "hermes"}:
                raise ConfigError("Choose a different backend: v1, v2 or hermes")
            if database.backend_version() not in {"v1", "v2"}:
                raise ConfigError("Preparing a switch from Hermes is not supported")
            # A failed retry must not leave an older snapshot authorized for use.
            database.invalidate_backend_switch()
            if database.has_unfinished_jobs():
                raise ConfigError("Finish or cancel every job before switching OpenCode versions")
            # The service lock does not cover children surviving a crashed
            # gateway. Do not snapshot a newly started server alongside one.
            for port in range(settings.opencode_port_start, settings.opencode_port_end + 1):
                family = socket.AF_INET6 if ":" in settings.opencode_host else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    try:
                        probe.bind((settings.opencode_host, port))
                    except OSError as exc:
                        raise ConfigError(
                            "An OpenCode runtime port is still occupied. Stop the old service "
                            "and all its worker processes before preparing the switch"
                        ) from exc
            groups: dict[str, list[dict]] = defaultdict(list)
            snapshots = {}
            for chat in database.backend_snapshot_chats():
                saved = database.saved_chat_history(chat["id"])
                if not database.can_use_chat_backend(chat) and saved is not None:
                    snapshots[chat["id"]] = saved
                else:
                    groups[chat["workspace_snapshot"]].append(chat)
            if groups:
                manager = OpenCodeManager(settings)
                try:
                    for workspace, chats in groups.items():
                        client = await manager.ensure(workspace)
                        statuses = await client.session_statuses()
                        if any(_busy(status) for status in statuses.values()):
                            raise ConfigError(
                                "OpenCode still has an active execution; finish it first"
                            )
                        if await client.questions() or await client.permissions():
                            raise ConfigError(
                                "Resolve pending OpenCode questions and permissions first"
                            )
                        for chat in chats:
                            messages = await client.messages(chat["opencode_session_id"])
                            ids = {message.get("info", message).get("id") for message in messages}
                            # Preserve locally recorded submissions too, including prompts
                            # that failed before they reached the source backend.
                            snapshots[chat["id"]] = messages + [
                                message for message in database.chat_submission_messages(chat["id"])
                                if message["info"]["id"] not in ids
                            ]
                finally:
                    await manager.close(strict=True)
            with database.transaction():
                for chat_id, messages in snapshots.items():
                    database.save_chat_history(chat_id, messages)
                database.prepare_backend_switch(target)
            return len(snapshots)
        finally:
            database.close()


def _busy(status) -> bool:
    if isinstance(status, dict):
        status = status.get("type") or status.get("status") or status.get("state")
    return not isinstance(status, str) or status.lower() not in {
        "idle",
        "completed",
        "failed",
        "aborted",
    }
