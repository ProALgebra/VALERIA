from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from opencode_web.database import Database


@pytest.fixture
def task_database(tmp_path: Path, workspace: Path):
    database = Database(tmp_path / "tasks.sqlite", "x" * 32)
    database.initialize()
    admin = database.create_user("admin", "long-admin-password", "admin", None)
    user = database.create_user(
        "runner", "long-user-password", "user", str(workspace), tasks_only=True
    )
    task = database.create_task(
        admin["id"],
        "Check CI",
        "Checks a pipeline",
        "Private instructions",
        "build",
        "test-provider",
        "test-model",
        "high",
        True,
    )
    yield database, admin, user, task
    database.close()


def start_task(
    database: Database,
    user: dict[str, Any],
    task: dict[str, Any],
    session_id: str | None,
    *,
    max_queued: int = 10,
    user_input: str = "My pipeline is 123",
) -> dict[str, Any]:
    return database.create_task_job(
        task=task,
        user=user,
        actor_user_id=user["id"],
        session_id=session_id,
        user_input=user_input,
        max_queued=max_queued,
    )


def test_tasks_only_user_updates_and_authentication(task_database) -> None:
    database, admin, user, _ = task_database
    assert user["tasks_only"] == 1
    assert admin["tasks_only"] == 0
    assert database.list_users()[1]["tasks_only"] == 1
    database.create_auth_session(user, "token", "csrf", 1)
    assert database.get_user_for_token("token")[0]["tasks_only"] == 1
    assert database.update_user(user["id"], tasks_only=False)["tasks_only"] == 0
    assert database.get_user_for_token("token")[0]["tasks_only"] == 0
    assert database.update_user(user["id"], tasks_only=True)["tasks_only"] == 1
    with pytest.raises(ValueError, match="administrator"):
        database.update_user(user["id"], role="admin")
    with pytest.raises(ValueError, match="administrator"):
        database.update_user(admin["id"], tasks_only=True)
    with pytest.raises(ValueError, match="administrator"):
        database.create_user("badadmin", "long-password", "admin", None, tasks_only=True)
    promoted = database.update_user(user["id"], role="admin", tasks_only=False)
    assert promoted["role"] == "admin"
    assert promoted["tasks_only"] == 0


def test_definition_crud_publication_and_soft_deletion(task_database) -> None:
    database, _, user, task = task_database
    assert database.list_tasks(published_only=True) == [task]
    updated = database.update_task(task["id"], {"title": " New task ", "is_published": False})
    assert updated["title"] == "New task"
    assert database.list_tasks(published_only=True) == []
    assert database.list_tasks(published_only=False) == [updated]
    with pytest.raises(ValueError, match="unavailable"):
        start_task(database, user, task, "stale-task")
    with pytest.raises(ValueError, match="required"):
        database.update_task(task["id"], {"content": "  "})
    with pytest.raises(ValueError, match="together"):
        database.update_task(task["id"], {"model_id": None})
    assert database.delete_task(task["id"])
    assert database.get_task(task["id"]) is None
    assert database.update_task(task["id"], {"title": "Revive"}) is None
    assert database.list_tasks(published_only=False) == []
    assert not database.delete_task(task["id"])


def test_task_sessions_hidden_and_snapshots_survive_definition_changes(task_database) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, "task-session")
    assert run["content"] == "Private instructions\n\nMy pipeline is 123"
    assert run["task_id"] == task["id"]
    assert run["task_title"] == "Check CI"
    assert run["agent"] == "build"
    assert run["provider_id"] == "test-provider"
    assert run["model_id"] == "test-model"
    assert run["model_variant"] == "high"
    assert run["user_input"] == "My pipeline is 123"
    assert database.get_chat(run["chat_id"])["kind"] == "task"
    assert database.list_chats() == []
    assert database.list_chats(user["id"], workspace=user["workspace_path"]) == []
    assert not database.user_owns_session(user["id"], user["workspace_path"], "task-session")
    assert database.list_task_runs(user["id"])[0]["id"] == run["id"]
    assert database.list_task_runs("another-user") == []
    database.update_task(task["id"], {"title": "Changed", "content": "New instructions"})
    database.delete_task(task["id"])
    assert database.get_task_run(run["id"]) == run
    assert database.list_jobs(user["id"])[0]["task_id"] == task["id"]
    database.claim_next_job(1)
    assert database.active_jobs()[0]["task_title"] == "Check CI"


@pytest.mark.parametrize("session_title", [None, "New session - 2026-09-21"])
def test_task_titles_preserve_initial_name_and_snapshot_on_restart(
    task_database, session_title: str | None
) -> None:
    database, _, user, task = task_database
    run = database.create_task_job(
        task=task,
        user=user,
        actor_user_id=user["id"],
        session_id="task-with-title",
        session_title=session_title,
        user_input="",
        max_queued=10,
    )
    initial_title = session_title if session_title is not None else task["title"]
    assert run["task_title"] == task["title"]
    assert run["task_initial_title"] == run["chat_title"] == initial_title
    original_chat = database.get_chat(run["chat_id"])
    generated_title = "Fix pipeline 123"
    database.set_chat_title(run["chat_id"], generated_title)
    updated = database.get_task_run(run["id"])
    assert updated == {**run, "chat_title": generated_title}
    assert database.get_chat(run["chat_id"]) == {**original_chat, "title": generated_title}
    database.update_task(task["id"], {"title": "Renamed template"})
    database.delete_task(task["id"])

    reopened = Database(database.path, "x" * 32)
    try:
        reopened.initialize()
        reopened.initialize()
        assert reopened.get_task_run(run["id"]) == updated
        assert reopened.list_task_runs(user["id"])[0] == updated
    finally:
        reopened.close()


def test_task_session_lookup_requires_matching_workspace_and_hidden_chat(task_database) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, "task-session")
    workspace = user["workspace_path"]
    assert database.get_task_run_for_session(workspace, "task-session") == run
    assert database.get_task_run_for_session(workspace + "/other", "task-session") is None
    assert database.get_task_run_for_session("", "task-session") is None
    assert database.get_task_run_for_session(workspace, "missing-session") is None
    chat = database.create_chat(user["id"], "chat-session", "Chat", workspace)
    database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="Message",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    assert database.get_task_run_for_session(workspace, "chat-session") is None


def test_initial_task_title_migration_backfills_once(task_database) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, "legacy-task-session")
    legacy_title = "Initial legacy session name"
    database.set_chat_title(run["chat_id"], legacy_title)
    with database.transaction() as connection:
        connection.execute("ALTER TABLE queue_jobs DROP COLUMN task_initial_title")
    database.initialize()
    migrated = database.get_task_run(run["id"])
    assert migrated["task_title"] == task["title"]
    assert migrated["task_initial_title"] == migrated["chat_title"] == legacy_title

    database.set_chat_title(run["chat_id"], "Generated title after upgrade")
    database.initialize()
    reopened = Database(database.path, "x" * 32)
    try:
        reopened.initialize()
        restored = reopened.get_task_run(run["id"])
        assert restored["task_initial_title"] == legacy_title
        assert restored["chat_title"] == "Generated title after upgrade"
        assert restored["task_title"] == task["title"]
    finally:
        reopened.close()


def test_session_creation_attempt_survives_requeue_and_restart_until_attached(
    task_database,
) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, None)
    assert run["session_creation_started"] == 0
    assert not database.mark_session_creation_started(run["id"])
    assert not database.mark_session_creation_started("missing-job")
    assert database.claim_next_job(1)["id"] == run["id"]
    assert database.mark_session_creation_started(run["id"])
    assert database.mark_session_creation_started(run["id"])
    assert database.get_job(run["id"])["session_creation_started"] == 1

    database.requeue_interrupted_job(run["id"])
    reopened = Database(database.path, "x" * 32)
    try:
        reopened.initialize()
        restored = reopened.claim_next_job(1)
        assert restored["id"] == run["id"]
        assert restored["session_pending"] == 1
        assert restored["session_creation_started"] == 1
        assert reopened.attach_task_session(run["id"], "recovered-session")
        attached = reopened.get_job(run["id"])
        assert attached["session_id"] == "recovered-session"
        assert attached["session_pending"] == 0
        assert attached["session_creation_started"] == 0
        assert not reopened.mark_session_creation_started(run["id"])
    finally:
        reopened.close()


def test_unsent_session_creation_is_safe_to_retry_after_restart(task_database) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, None)
    assert database.claim_next_job(1)["id"] == run["id"]
    assert database.set_session_creation_started(run["id"], True)
    assert database.set_session_creation_started(run["id"], False)
    database.requeue_interrupted_job(run["id"])
    reopened = Database(database.path, "x" * 32)
    try:
        reopened.initialize()
        restored = reopened.claim_next_job(1)
        assert restored["id"] == run["id"]
        assert restored["session_pending"] == 1
        assert restored["session_creation_started"] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "status",
    ["running", "waiting_permission", "waiting_question", "cancelling", "cancelled",
     "completed", "failed"],
)
def test_session_creation_attempt_requires_pending_dispatching_job(task_database, status) -> None:
    database, _, user, task = task_database
    run = start_task(database, user, task, None)
    database.set_job_status(run["id"], status)
    assert not database.mark_session_creation_started(run["id"])
    assert database.get_job(run["id"])["session_creation_started"] == 0


def test_tasks_share_chat_fifo_and_concurrency_limit(task_database) -> None:
    database, _, user, task = task_database
    chat = database.create_chat(user["id"], "chat-session", "Chat", user["workspace_path"])
    first = database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="Chat request",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    second = start_task(database, user, task, "task-session")
    assert database.get_task_run(first["id"]) is None
    assert database.user_owns_session(user["id"], user["workspace_path"], "chat-session")
    assert [item["id"] for item in database.list_chats()] == [chat["id"]]
    assert database.claim_next_job(1)["id"] == first["id"]
    assert database.claim_next_job(1) is None
    database.set_job_status(first["id"], "completed")
    assert database.claim_next_job(1)["id"] == second["id"]
    database.set_job_status(second["id"], "completed")
    assert database.get_task_run(second["id"])["status"] == "completed"


def test_queue_capacity_is_atomic_across_connections(task_database) -> None:
    database, _, user, task = task_database
    second_database = Database(database.path, "x" * 32)
    second_database.initialize()
    barrier = Barrier(2)

    def submit(connection: Database, session: str) -> dict[str, Any] | str:
        barrier.wait(timeout=10)
        try:
            return start_task(connection, user, task, session, max_queued=1, user_input=session)
        except ValueError as error:
            return str(error)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(submit, database, "first"),
                executor.submit(submit, second_database, "second"),
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "The queue is full" in results
        assert database.queued_count() == 1
        assert len(database.list_task_runs()) == 1
        assert database._one("SELECT COUNT(*) AS total FROM chats")["total"] == 1
    finally:
        second_database.close()


@pytest.mark.parametrize(
    "status", ["queued", "dispatching", "running", "waiting_permission", "cancelling"]
)
def test_duplicate_task_is_cancelled_before_dispatch(task_database, status):
    database, _, user, task = task_database
    original = start_task(database, user, task, "original")
    database.set_job_status(original["id"], status)
    original = database.get_job(original["id"])
    duplicate = start_task(database, user, task, "duplicate", max_queued=1)
    assert duplicate["status"] == "cancelled"
    assert duplicate["error_code"] == "duplicate_task"
    assert duplicate["started_at"] is None
    assert duplicate["finished_at"] == duplicate["created_at"]
    assert database.get_chat(duplicate["chat_id"])["status"] == "idle"
    assert database.get_job(original["id"]) == original
    if status == "queued":
        assert database.claim_next_job(10)["id"] == original["id"]
    assert database.claim_next_job(10) is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_task_does_not_block_a_repeat(task_database, status):
    database, _, user, task = task_database
    original = start_task(database, user, task, "original")
    database.set_job_status(original["id"], status)
    repeat = start_task(database, user, task, "repeat")
    assert repeat["status"] == "queued"
    assert repeat["error_code"] is None
    assert database.claim_next_job(10)["id"] == repeat["id"]


def test_duplicate_check_uses_full_input_and_template_id(task_database):
    database, admin, user, task = task_database
    prefix = "x" * 120
    first = start_task(database, user, task, "first", user_input=prefix + "1")
    different_input = start_task(database, user, task, "second", user_input=prefix + "2")
    other_task = database.create_task(
        admin["id"], task["title"], "", "Other instructions", "build",
        "test-provider", "test-model", None, True,
    )
    different_template = start_task(
        database, user, other_task, "third", user_input=prefix + "1"
    )
    empty = start_task(database, user, task, "empty", user_input="")
    whitespace = start_task(database, user, task, "whitespace", user_input=" \n ")
    for run in (first, different_input, different_template, empty):
        assert run["status"] == "queued"
    assert whitespace["status"] == "cancelled"


def test_simultaneous_duplicate_submissions_have_one_queued_run(task_database):
    database, _, user, task = task_database
    second_database = Database(database.path, "x" * 32)
    second_database.initialize()
    barrier = Barrier(2)

    def submit(connection, session):
        barrier.wait(timeout=10)
        return start_task(connection, user, task, session, max_queued=1)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(submit, database, "first"),
                executor.submit(submit, second_database, "second"),
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sorted(run["status"] for run in results) == ["cancelled", "queued"]
        assert database.queued_count() == 1
        assert len(database.list_task_runs()) == 2
        queued = next(run for run in results if run["status"] == "queued")
        assert database.claim_next_job(10)["id"] == queued["id"]
        assert database.claim_next_job(10) is None
    finally:
        second_database.close()


def test_failed_job_insert_rolls_back_hidden_chat(task_database) -> None:
    database, _, user, task = task_database
    with pytest.raises(sqlite3.IntegrityError):
        database.create_task_job(
            task=task,
            user=user,
            actor_user_id="missing-user",
            session_id="no-chat",
            user_input="",
            max_queued=10,
        )
    assert database.list_task_runs() == []
    assert database._one("SELECT COUNT(*) AS total FROM chats")["total"] == 0
    run = database.create_task_job(
        task=task,
        user=user,
        actor_user_id=user["id"],
        session_id="valid-session",
        user_input="",
        max_queued=10,
    )
    assert run["content"] == task["content"]


@pytest.mark.parametrize("change", ["workspace", "disabled", "password", "task"])
def test_enqueue_rejects_changes_during_session_creation(task_database, change: str) -> None:
    database, _, user, task = task_database
    if change == "workspace":
        database.update_user(user["id"], workspace_path=user["workspace_path"] + "/moved")
    elif change == "disabled":
        database.update_user(user["id"], status="disabled")
    elif change == "password":
        database.reset_password(user["id"], "a-new-long-password")
    else:
        database.update_task(task["id"], {"content": "Updated administrator instructions"})
    with pytest.raises(ValueError, match="changed"):
        start_task(database, user, task, "created-before-change")
    assert database.list_task_runs() == []
    assert database._one("SELECT COUNT(*) AS total FROM chats")["total"] == 0


@pytest.mark.parametrize("creation_started", [None, 1])
def test_initialize_migrates_existing_tables_without_changing_data(
    tmp_path: Path, creation_started: int | None,
) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, login TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL, workspace_path TEXT, status TEXT NOT NULL DEFAULT 'active',
            auth_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_login_at TEXT
        );
        CREATE TABLE chats (
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES users(id),
            opencode_session_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            workspace_snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'idle',
            archived_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE queue_jobs (
            id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
            chat_id TEXT NOT NULL REFERENCES chats(id), session_id TEXT NOT NULL,
            effective_user_id TEXT NOT NULL REFERENCES users(id),
            actor_user_id TEXT NOT NULL REFERENCES users(id), workspace_snapshot TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE, content TEXT NOT NULL, agent TEXT NOT NULL,
            provider_id TEXT, model_id TEXT, model_variant TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_code TEXT
        );
        CREATE TABLE task_run_results (
            job_id TEXT PRIMARY KEY REFERENCES queue_jobs(id) ON DELETE CASCADE,
            message_id TEXT, text TEXT, saved_at TEXT NOT NULL
        );
        INSERT INTO users (id, login, password_hash, role, workspace_path, created_at, updated_at)
            VALUES ('user', 'legacy', 'hash', 'user', '/workspace', 'now', 'now');
        INSERT INTO chats
            (id, owner_user_id, opencode_session_id, title, workspace_snapshot,
             created_at, updated_at)
            VALUES ('chat', 'user', 'session', 'Legacy chat', '/workspace', 'now', 'now');
        INSERT INTO queue_jobs
            (id, sequence, chat_id, session_id, effective_user_id, actor_user_id,
             workspace_snapshot, message_id, content, agent, status, created_at)
            VALUES ('job', 1, 'chat', 'session', 'user', 'user', '/workspace',
                    'msg_legacy', 'Legacy prompt', 'build', 'queued', 'now');
        INSERT INTO task_run_results(job_id, message_id, text, saved_at)
            VALUES ('job', 'msg_result', 'Legacy result', 'now');
    """)
    if creation_started is not None:
        connection.execute(
            "ALTER TABLE queue_jobs ADD COLUMN session_creation_started INTEGER NOT NULL DEFAULT 0"
        )
        connection.execute(
            "UPDATE queue_jobs SET session_creation_started = ?", (creation_started,)
        )
        connection.commit()
    connection.close()
    database = Database(path, "x" * 32)
    try:
        database.initialize()
        database.initialize()
        assert database.get_user("user")["tasks_only"] == 0
        assert database.get_chat("chat")["kind"] == "chat"
        assert database.get_chat("chat")["preferred_agent"] == "build"
        assert database.list_chats("user")[0]["title"] == "Legacy chat"
        assert database.get_job("job")["task_id"] is None
        assert database.get_job("job")["content"] == "Legacy prompt"
        assert database.get_job("job")["resume_monitor"] == 0
        assert database.get_job("job")["session_creation_started"] == (creation_started or 0)
        assert database.get_job("job")["queue_id"] == "default"
        assert database.get_work_queue("default")["name"] == "Основная"
        assert database.get_task_result("job")["text"] == "Legacy result"
        database.set_job_status("job", "waiting_question")
        assert database.get_job("job")["status"] == "waiting_question"
        assert database._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.list_task_runs() == []
    finally:
        database.close()
