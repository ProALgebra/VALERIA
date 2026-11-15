from __future__ import annotations

import socket
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencode_web.app import create_app
from opencode_web.backend_switch import database_process_lock, prepare_backend_switch
from opencode_web.backends import OpenCodeError
from opencode_web.config import ConfigError
from opencode_web.database import Database, ExecutionModeChangingError


@pytest.fixture
def stored(settings, workspace):
    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    db.activate_backend("v1")
    user = db.create_user("worker", "long-enough-password", "user", str(workspace))
    chat = db.create_chat(user["id"], "ses_old", "Old history", str(workspace))
    yield SimpleNamespace(db=db, user=user, chat=chat)
    db.close()


def enqueue(t, chat):
    return t.db.create_job(
        chat=chat,
        effective_user_id=t.user["id"],
        actor_user_id=t.user["id"],
        content="Hello",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )


def test_switch_requires_preparation_and_snapshots(stored):
    db = stored.db
    with pytest.raises(ValueError, match="backend-switch"):
        db.activate_backend("v2")
    db.prepare_backend_switch("v2")
    with pytest.raises(ValueError, match="snapshot is missing"):
        db.activate_backend("v2")
    assert db.backend_version() == "v1"


def test_switch_preserves_history_and_versions_new_jobs(stored, workspace):
    t = stored
    history = [
        {
            "info": {"id": "msg_old", "role": "assistant"},
            "parts": [{"type": "text", "text": "Old answer"}],
        }
    ]
    t.db.save_chat_history(t.chat["id"], history)
    t.db.prepare_backend_switch("v2")
    t.db.activate_backend("v2")
    old = t.db.get_chat(t.chat["id"])
    assert old["backend_version"] == "v1"
    assert old["archived_reason"] == "backend_version_changed"
    assert not t.db.can_use_chat_backend(old)
    assert t.db.saved_chat_history(old["id"]) == history
    with pytest.raises(ExecutionModeChangingError):
        enqueue(t, old)
    new = t.db.create_chat(t.user["id"], None, "New", str(workspace))
    assert new["backend_version"] == "v2"
    assert enqueue(t, new)["backend_version"] == "v2"


def test_unfinished_jobs_prevent_switch(stored):
    enqueue(stored, stored.chat)
    with pytest.raises(ValueError, match="Finish or cancel"):
        stored.db.prepare_backend_switch("v2")
    assert stored.db.backend_version() == "v1"


def test_restarting_source_backend_invalidates_prepared_transition(stored):
    stored.db.save_chat_history(stored.chat["id"], [])
    stored.db.prepare_backend_switch("v2")
    stored.db.activate_backend("v1")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("v2")


def test_service_and_prepare_command_share_exclusive_lock(settings):
    with (
        database_process_lock(settings.database_path),
        pytest.raises(ConfigError, match="Stop the OpenCode Web service"),
        database_process_lock(settings.database_path),
    ):
        pytest.fail("A second service or preparation command must not run")


async def test_offline_prepare_snapshots_before_activation(stored, settings, monkeypatch):
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(return_value=[{"info": {"id": "msg_old"}, "parts": []}]),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=client), close=AsyncMock())
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    assert await prepare_backend_switch(settings, "v2") == 1
    assert stored.db.saved_chat_history(stored.chat["id"])[0]["info"]["id"] == "msg_old"
    assert stored.db.backend_version() == "v1"
    manager.close.assert_awaited_once()
    stored.db.activate_backend("v2")
    assert stored.db.backend_version() == "v2"


async def test_prepare_failure_does_not_authorize_switch(stored, settings, monkeypatch):
    manager = SimpleNamespace(
        ensure=AsyncMock(
            return_value=SimpleNamespace(
                session_statuses=AsyncMock(return_value={"ses_old": {"type": "busy"}})
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    with pytest.raises(ConfigError, match="active execution"):
        await prepare_backend_switch(settings, "v2")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("v2")
    manager.close.assert_awaited_once()


async def test_startup_rejects_unprepared_switch_before_runtime_start(stored, settings):
    app = create_app(replace(settings, opencode_api_version="v2"), start_dispatcher=False)
    with pytest.raises(ConfigError, match="backend-switch"):
        async with app.router.lifespan_context(app):
            pytest.fail("Unprepared version transition must not start the server")


async def test_orphan_runtime_port_blocks_preparation(stored, settings):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = replace(settings, opencode_port_start=port, opencode_port_end=port)
        with pytest.raises(ConfigError, match="runtime port is still occupied"):
            await prepare_backend_switch(config, "v2")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("v2")


async def test_cleanup_failure_does_not_authorize_switch(stored, settings, monkeypatch):
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(return_value=[]),
    )
    manager = SimpleNamespace(
        ensure=AsyncMock(return_value=client),
        close=AsyncMock(side_effect=OpenCodeError("cleanup")),
    )
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    with pytest.raises(OpenCodeError, match="cleanup"):
        await prepare_backend_switch(settings, "v2")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("v2")
