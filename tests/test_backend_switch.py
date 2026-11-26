from __future__ import annotations

import socket
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.backend_switch import database_process_lock, prepare_backend_switch
from opencode_web.backends import OpenCodeError
from opencode_web.config import ConfigError
from opencode_web.database import BackendVersionChangedError, Database


@pytest.fixture
def stored(settings, workspace, request):
    db = Database(settings.database_path, settings.session_secret)
    db.initialize()
    db.activate_backend(getattr(request, "param", "v1"))
    user = db.create_user("worker", "long-enough-password", "user", str(workspace))
    chat = db.create_chat(user["id"], "ses_old", "Old history", str(workspace))
    yield SimpleNamespace(db=db, user=user, chat=chat)
    db.close()


@pytest.fixture(params=["v2", "hermes"])
def target(request):
    return request.param


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


def test_switch_requires_preparation_and_snapshots(stored, target):
    db = stored.db
    with pytest.raises(ValueError, match="backend-switch"):
        db.activate_backend(target)
    db.prepare_backend_switch(target)
    with pytest.raises(ValueError, match="snapshot is missing"):
        db.activate_backend(target)
    assert db.backend_version() == "v1"


def test_switch_preserves_history_and_versions_new_jobs(stored, workspace, target):
    t = stored
    history = [
        {
            "info": {"id": "msg_old", "role": "assistant"},
            "parts": [{"type": "text", "text": "Old answer"}],
        }
    ]
    t.db.save_chat_history(t.chat["id"], history)
    t.db.prepare_backend_switch(target)
    t.db.activate_backend(target)
    old = t.db.get_chat(t.chat["id"])
    assert old["backend_version"] == "v1"
    assert old["archived_reason"] == "backend_version_changed"
    assert not t.db.can_use_chat_backend(old)
    assert t.db.saved_chat_history(old["id"]) == history
    with pytest.raises(BackendVersionChangedError):
        enqueue(t, old)
    new = t.db.create_chat(t.user["id"], None, "New", str(workspace))
    assert new["backend_version"] == target
    assert enqueue(t, new)["backend_version"] == target


def test_unfinished_jobs_prevent_switch(stored, target):
    enqueue(stored, stored.chat)
    with pytest.raises(ValueError, match="Finish or cancel"):
        stored.db.prepare_backend_switch(target)
    assert stored.db.backend_version() == "v1"


def test_restarting_source_backend_invalidates_prepared_transition(stored, target):
    stored.db.save_chat_history(stored.chat["id"], [])
    stored.db.prepare_backend_switch(target)
    stored.db.activate_backend("v1")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend(target)


def test_service_and_prepare_command_share_exclusive_lock(settings):
    with (
        database_process_lock(settings.database_path),
        pytest.raises(ConfigError, match="Stop the OpenCode Web service"),
        database_process_lock(settings.database_path),
    ):
        pytest.fail("A second service or preparation command must not run")


async def test_offline_prepare_snapshots_before_activation(stored, settings, monkeypatch, target):
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(return_value=[{"info": {"id": "msg_old"}, "parts": []}]),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=client), close=AsyncMock())
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    assert await prepare_backend_switch(settings, target) == 1
    assert stored.db.saved_chat_history(stored.chat["id"])[0]["info"]["id"] == "msg_old"
    assert stored.db.backend_version() == "v1"
    manager.close.assert_awaited_once()
    stored.db.activate_backend(target)
    assert stored.db.backend_version() == target


async def test_prepare_failure_does_not_authorize_switch(stored, settings, monkeypatch, target):
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
        await prepare_backend_switch(settings, target)
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend(target)
    manager.close.assert_awaited_once()


async def test_startup_rejects_unprepared_switch_before_runtime_start(stored, settings, target):
    app = create_app(
        replace(settings, opencode_api_version=target, hermes_api_key="test-key"),
        start_dispatcher=False,
    )
    with pytest.raises(ConfigError, match="backend-switch"):
        async with app.router.lifespan_context(app):
            pytest.fail("Unprepared version transition must not start the server")


async def test_existing_v1_database_starts_without_switch_or_session_recreation(
    stored, settings,
):
    job = enqueue(stored, stored.chat)
    # Reproduce the published schema before backend versioning was introduced.
    with stored.db.transaction() as connection:
        connection.execute("ALTER TABLE chats DROP COLUMN backend_version")
        connection.execute("ALTER TABLE queue_jobs DROP COLUMN backend_version")
        connection.execute("DROP TABLE chat_history_snapshots")
        connection.execute("DELETE FROM settings WHERE key LIKE 'opencode_%'")
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        previous_chat = db.get_chat(stored.chat["id"])
        previous_job = db.get_job(job["id"])
        assert db.backend_version() == "v1"
        assert previous_chat["archived_reason"] is None
        assert previous_chat["opencode_session_id"] == "ses_old"
        assert db.can_use_chat_backend(previous_chat)
        assert previous_job["backend_version"] == "v1"
        assert previous_job["session_id"] == "ses_old"
        assert previous_job["status"] == "queued"
        assert not previous_job["session_pending"]
        assert app.state.manager.instances() == []


async def test_orphan_runtime_port_blocks_preparation(stored, settings, target):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = replace(settings, opencode_port_start=port, opencode_port_end=port)
        with pytest.raises(ConfigError, match="runtime port is still occupied"):
            await prepare_backend_switch(config, target)
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend(target)


async def test_cleanup_failure_does_not_authorize_switch(stored, settings, monkeypatch, target):
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
        await prepare_backend_switch(settings, target)
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend(target)


async def test_failed_retry_invalidates_previous_preparation(stored, settings, monkeypatch):
    stored.db.save_chat_history(stored.chat["id"], [])
    stored.db.prepare_backend_switch("hermes")
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(side_effect=OpenCodeError("history unavailable")),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=client), close=AsyncMock())
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    with pytest.raises(OpenCodeError, match="history unavailable"):
        await prepare_backend_switch(settings, "hermes")
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("hermes")
    assert stored.db.get_chat(stored.chat["id"])["archived_reason"] is None
    manager.close.assert_awaited_once_with(strict=True)


async def test_partial_history_failure_does_not_save_or_prepare(stored, settings, monkeypatch):
    second = stored.db.create_chat(
        stored.user["id"], "ses_second", "Second", stored.user["workspace_path"]
    )
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(side_effect=[[{"info": {"id": "old"}}], OpenCodeError("history")]),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=client), close=AsyncMock())
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    with pytest.raises(OpenCodeError, match="history"):
        await prepare_backend_switch(settings, "hermes")
    assert stored.db.saved_chat_history(stored.chat["id"]) is None
    assert stored.db.saved_chat_history(second["id"]) is None
    with pytest.raises(ValueError, match="backend-switch"):
        stored.db.activate_backend("hermes")


@pytest.mark.parametrize("stored", ["v1", "v2"], indirect=True)
@pytest.mark.parametrize("cached_result", [True, False])
async def test_migrate_to_hermes_preserves_data_and_archived_api(
    stored, settings, monkeypatch, cached_result,
):
    db, user, chat = stored.db, stored.user, stored.chat
    source = db.backend_version()
    old_settings = replace(settings, opencode_api_version=source)
    admin = db.create_user("operator", "operator-password", "admin", None)
    template = db.create_template(admin["id"], "Template", None, "Template content", True)
    task = db.create_task(admin["id"], "Task", None, "Do work", "build", None, None, None, True)
    run = db.create_task_job(
        task=task, user=user, actor_user_id=user["id"], session_id="ses_task",
        user_input="Old input", max_queued=100,
    )
    db.complete_task_run(run["id"], {"message_id": "answer", "text": "Old task result"})
    if not cached_result:
        with db.transaction() as connection:
            connection.execute("DELETE FROM task_run_results WHERE job_id = ?", (run["id"],))
    consultation = db.create_chat(
        user["id"], "ses_consultation", "Consultation", user["workspace_path"], consultation=True
    )
    consultation_job = enqueue(stored, consultation)
    db.set_job_status(consultation_job["id"], "completed")
    db.save_consultation_answer(consultation_job["id"], "Old consultation answer")
    db.set_consultation_model("test/model", admin["id"])
    failed = enqueue(stored, chat)
    db.set_job_status(failed["id"], "failed")
    pending = db.create_chat(user["id"], None, "Never started", user["workspace_path"])
    cancelled = enqueue(stored, pending)
    db.set_job_status(cancelled["id"], "cancelled")
    db.ensure_default_queue_limit(settings.max_active_sessions_hard_limit)
    queues = db.list_work_queues()
    histories = {
        "ses_old": [{
            "info": {"id": "old_answer", "role": "assistant"},
            "parts": [{"type": "text", "text": "Old answer"}],
        }],
        "ses_task": [
            {"info": {"id": run["message_id"], "role": "user"}, "parts": []},
            {
                "info": {
                    "id": "answer", "role": "assistant", "parentID": run["message_id"],
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "Old task result"}],
            },
        ],
    }
    source_client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(side_effect=lambda session_id: histories[session_id]),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=source_client), close=AsyncMock())
    monkeypatch.setattr("opencode_web.backend_switch.OpenCodeManager", lambda *a, **kw: manager)
    assert await prepare_backend_switch(old_settings, "hermes") == 2
    assert db.backend_version() == source
    assert db.get_chat(chat["id"])["archived_reason"] is None
    manager.close.assert_awaited_once_with(strict=True)

    app = create_app(
        replace(settings, opencode_api_version="hermes", hermes_api_key="test-key"),
        start_dispatcher=False,
    )
    async with app.router.lifespan_context(app):
        # Every archived endpoint must work without starting any backend process.
        ensure = AsyncMock(side_effect=AssertionError("Archived history must use SQLite"))
        monkeypatch.setattr(app.state.manager, "ensure", ensure)
        migrated = app.state.database
        assert migrated.backend_version() == "hermes"
        assert migrated.get_user(user["id"]) == user
        assert migrated.get_template(template["id"]) == template
        assert migrated.get_task(task["id"]) == task
        assert migrated.list_work_queues() == queues
        assert migrated.consultation_model() == "test/model"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/auth/login", json={"login": "worker", "password": "long-enough-password"}
            )
            assert login.status_code == 200
            headers = {"x-csrf-token": login.json()["csrf_token"]}
            listing = await client.get("/api/chats")
            assert chat["id"] in {item["id"] for item in listing.json()}
            detail = await client.get(f"/api/chats/{chat['id']}")
            assert detail.json()["archived_reason"] == "backend_version_changed"
            messages = await client.get(f"/api/chats/{chat['id']}/messages")
            assert messages.status_code == 200
            assert messages.json()[0]["parts"][0]["text"] == "Old answer"
            assert failed["message_id"] in {m["info"]["id"] for m in messages.json()}
            pending_messages = await client.get(f"/api/chats/{pending['id']}/messages")
            assert pending_messages.json()[0]["info"]["id"] == cancelled["message_id"]
            old_result = await client.get(f"/api/task-runs/{run['id']}/result")
            assert old_result.json()["text"] == "Old task result"
            old_consultation = await client.get(f"/api/consultations/{consultation['id']}")
            assert old_consultation.json()["archived_reason"] == "backend_version_changed"
            assert old_consultation.json()["messages"][-1]["text"] == "Old consultation answer"
            rejected = await client.post(
                f"/api/chats/{chat['id']}/messages", headers=headers, json={"message": "Continue"}
            )
            assert rejected.status_code == 409
            rejected_consultation = await client.post(
                f"/api/consultations/{consultation['id']}/messages",
                headers=headers, json={"message": "Continue"},
            )
            assert rejected_consultation.status_code == 409
            ensure.assert_not_awaited()
            new = await client.post("/api/chats", headers=headers, json={"message": "New request"})
            assert new.status_code == 201, new.text
            created = new.json()
            assert created["chat"]["backend_version"] == "hermes"
            assert created["chat"]["archived_reason"] is None
            assert created["job"]["backend_version"] == "hermes"
            assert created["job"]["session_pending"]
            assert created["job"]["session_id"] != chat["opencode_session_id"]
            new_task = migrated.create_task_job(
                task=task, user=user, actor_user_id=user["id"], session_id=None,
                user_input="New task input", max_queued=100,
            )
            assert new_task["backend_version"] == "hermes"
            assert new_task["session_pending"]
            new_consultation = await client.post(
                "/api/consultations", headers=headers, json={"message": "New question"}
            )
            assert new_consultation.status_code == 201, new_consultation.text
            new_chat = migrated.get_chat(new_consultation.json()["id"])
            assert new_chat["backend_version"] == "hermes"
            assert new_chat["archived_reason"] is None


def test_cli_accepts_hermes_target(settings, monkeypatch, capsys):
    from opencode_web.cli import main

    prepare = AsyncMock(return_value=3)
    monkeypatch.setattr(sys, "argv", ["opencode-web", "backend-switch", "--to", "hermes"])
    monkeypatch.setattr("opencode_web.cli.load_dotenv", lambda: None)
    monkeypatch.setattr("opencode_web.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr("opencode_web.backend_switch.prepare_backend_switch", prepare)
    main()
    prepare.assert_awaited_once_with(settings, "hermes")
    assert "Saved 3 histories" in capsys.readouterr().out
