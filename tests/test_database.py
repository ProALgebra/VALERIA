from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opencode_web.database import DEFAULT_QUEUE_ID, Database
from opencode_web.security import verify_password


def test_users_sessions_templates_and_queue(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "db.sqlite", "x" * 32)
    database.initialize()
    admin = database.create_user("Admin", "correct-horse-battery", "admin", None)
    user = database.create_user("Ivanov", "another-correct-password", "user", str(workspace))

    assert admin["login"] == "admin"
    assert verify_password(user["password_hash"], "another-correct-password")

    template = database.create_template(admin["id"], "CI", None, "Проверь CI", True)
    assert database.list_templates(published_only=True)[0]["id"] == template["id"]

    chat_a = database.create_chat(user["id"], "session-a", "A", str(workspace))
    chat_b = database.create_chat(user["id"], "session-b", "B", str(workspace))
    assert database.set_chat_title(chat_a["id"], "Title from OpenCode")["title"] == (
        "Title from OpenCode"
    )
    first = database.create_job(
        chat=chat_a,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="first",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    second = database.create_job(
        chat=chat_b,
        effective_user_id=user["id"],
        actor_user_id=admin["id"],
        content="second",
        agent="plan",
        provider_id="openai",
        model_id="gpt-test",
        model_variant=None,
    )
    assert first["message_id"].startswith("msg_")
    assert second["message_id"].startswith("msg_")

    claimed = database.claim_next_job(1)
    assert claimed and claimed["id"] == first["id"]
    assert database.claim_next_job(1) is None
    database.set_job_status(first["id"], "completed")
    claimed = database.claim_next_job(1)
    assert claimed and claimed["id"] == second["id"]
    assert claimed["actor_login"] == "admin"
    assert claimed["effective_login"] == "ivanov"
    database.close()


def test_messages_in_same_session_remain_ordered(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "queue.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    chat = database.create_chat(user["id"], "same-session", "Chat", str(workspace))
    ids = []
    for content in ("one", "two"):
        job = database.create_job(
            chat=chat,
            effective_user_id=user["id"],
            actor_user_id=user["id"],
            content=content,
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        ids.append(job["id"])

    assert database.claim_next_job(2)["id"] == ids[0]
    assert database.claim_next_job(2) is None
    database.set_job_status(ids[0], "completed")
    assert database.claim_next_job(2)["id"] == ids[1]
    database.close()


def test_waiting_question_releases_slot_but_blocks_its_session(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "questions.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    waiting = _job(database, user["id"], "session-a", "first", str(workspace))
    same_session = database.create_job(
        chat=database.get_chat(waiting["chat_id"]),
        effective_user_id=user["id"], actor_user_id=user["id"], content="second",
        agent="plan", provider_id=None, model_id=None, model_variant=None,
    )
    other = _job(database, user["id"], "session-b", "other", str(workspace))
    assert database.claim_next_job(1)["id"] == waiting["id"]
    database.set_job_status(waiting["id"], "waiting_question")
    assert database.active_slot_count() == 0
    assert database.claim_next_job(1)["id"] == other["id"]
    assert database.claim_next_job(2) is None
    database.set_job_status(other["id"], "completed")
    assert database.queue_consultation_resume(waiting["id"]) is False
    # It is not a consultation, so its later message stays blocked until terminal.
    database.set_job_status(waiting["id"], "completed")
    assert database.claim_next_job(1)["id"] == same_session["id"]
    database.close()


def test_initialize_migrates_legacy_queued_message_id(tmp_path: Path, workspace: Path) -> None:
    path = tmp_path / "legacy-message-id.sqlite"
    database = Database(path, "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    chat = database.create_chat(user["id"], "legacy-session", "Legacy", str(workspace))
    job = database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="queued before upgrade",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    database.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE queue_jobs SET message_id = ? WHERE id = ?",
        ("74683076-adf5-45ee-a364-5997e9d7a47b", job["id"]),
    )
    connection.commit()
    connection.close()

    database = Database(path, "x" * 32)
    database.initialize()
    assert database.get_job(job["id"])["message_id"] == ("msg_74683076adf545eea3645997e9d7a47b")
    database.close()


def test_cancelling_last_queued_job_returns_chat_to_idle(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "cancel.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    chat = database.create_chat(user["id"], "cancel-session", "Chat", str(workspace))
    job = database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="cancel me",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )

    assert database.cancel_queued_job(job["id"], user["id"])
    assert database.get_chat(chat["id"])["status"] == "idle"
    database.close()


def _job(
    database: Database,
    user_id: str,
    session: str,
    content: str,
    workspace: str,
    *,
    queue_id: str = DEFAULT_QUEUE_ID,
) -> dict:
    chat = database.create_chat(user_id, session, session, workspace)
    return database.create_job(
        chat=chat,
        effective_user_id=user_id,
        actor_user_id=user_id,
        content=content,
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
        queue_id=queue_id,
    )


def test_claim_next_job_respects_per_user_limit(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "per-user.sqlite", "x" * 32)
    database.initialize()
    limited = database.create_user("limited", "long-enough-password", "user", str(workspace))
    other = database.create_user("other", "long-enough-password", "user", str(workspace))
    database.update_user(limited["id"], max_active_jobs=1)
    assert database.get_user(limited["id"])["max_active_jobs"] == 1
    assert database.get_user(other["id"])["max_active_jobs"] is None

    first = _job(database, limited["id"], "sess-a", "a", str(workspace))
    blocked = _job(database, limited["id"], "sess-b", "b", str(workspace))
    unrelated = _job(database, other["id"], "sess-c", "c", str(workspace))

    assert database.claim_next_job(3)["id"] == first["id"]
    # limited is at its per-user cap: its queued job is skipped, other user proceeds.
    assert database.claim_next_job(3)["id"] == unrelated["id"]
    assert database.claim_next_job(3) is None
    database.set_job_status(first["id"], "completed")
    assert database.claim_next_job(3)["id"] == blocked["id"]

    database.update_user(limited["id"], max_active_jobs=None)
    assert database.get_user(limited["id"])["max_active_jobs"] is None
    database.close()


def test_claim_next_job_applies_per_user_default(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "per-user-default.sqlite", "x" * 32)
    database.initialize()
    first_user = database.create_user("one", "long-enough-password", "user", str(workspace))
    second_user = database.create_user("two", "long-enough-password", "user", str(workspace))
    root = str(workspace)

    a1 = _job(database, first_user["id"], "sess-a1", "a1", root)
    a2 = _job(database, first_user["id"], "sess-a2", "a2", root)
    b1 = _job(database, second_user["id"], "sess-b1", "b1", root)

    assert database.claim_next_job(3, per_user_default=1)["id"] == a1["id"]
    assert database.claim_next_job(3, per_user_default=1)["id"] == b1["id"]
    assert database.claim_next_job(3, per_user_default=1) is None
    # Without a default only the global limit applies.
    database.set_job_status(a1["id"], "completed")
    assert database.claim_next_job(3)["id"] == a2["id"]
    database.close()


def test_update_user_validates_max_active_jobs(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "user-limit.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    with pytest.raises(ValueError):
        database.update_user(user["id"], max_active_jobs=0)
    assert database.get_user(user["id"])["max_active_jobs"] is None
    database.close()


def test_priority_queues_use_greedy_limits_and_fifo(tmp_path: Path, workspace: Path) -> None:
    database = Database(tmp_path / "priority-queues.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    urgent = database.create_work_queue("Срочные", 100, 1)
    background = database.create_work_queue("Фоновые", 10, 2)

    low_first = database.create_job(
        chat=database.create_chat(user["id"], "low-1", "Low 1", str(workspace)),
        effective_user_id=user["id"], actor_user_id=user["id"], content="low-1",
        agent="build", provider_id=None, model_id=None, model_variant=None,
        queue_id=background["id"],
    )
    high_first = database.create_job(
        chat=database.create_chat(user["id"], "high-1", "High 1", str(workspace)),
        effective_user_id=user["id"], actor_user_id=user["id"], content="high-1",
        agent="build", provider_id=None, model_id=None, model_variant=None,
        queue_id=urgent["id"],
    )
    high_second = database.create_job(
        chat=database.create_chat(user["id"], "high-2", "High 2", str(workspace)),
        effective_user_id=user["id"], actor_user_id=user["id"], content="high-2",
        agent="build", provider_id=None, model_id=None, model_variant=None,
        queue_id=urgent["id"],
    )

    assert database.claim_next_job(3)["id"] == high_first["id"]
    # The urgent queue is at its own cap, so an unused global slot goes to lower priority.
    assert database.claim_next_job(3)["id"] == low_first["id"]
    assert database.claim_next_job(3) is None
    database.set_job_status(high_first["id"], "completed")
    assert database.claim_next_job(3)["id"] == high_second["id"]
    database.close()


def test_reserved_worker_is_not_taken_by_other_queues(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "reserved-worker.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    reserved = database.create_work_queue("Резерв", 0, 2, reserved_workers=1)
    shared = database.create_work_queue("Общая", 100, 5)
    shared_jobs = [
        _job(database, user["id"], f"shared-{index}", f"shared-{index}",
             str(workspace), queue_id=shared["id"])
        for index in range(4)
    ]
    reserved_job = _job(
        database, user["id"], "reserved", "reserved", str(workspace),
        queue_id=reserved["id"],
    )

    claimed = []
    while job := database.claim_next_job(4):
        claimed.append(job["id"])

    # The shared queue fills only three shared slots; the fourth global slot is
    # reserved for the other queue even though it has just one queued job.
    assert claimed == [
        shared_jobs[0]["id"],
        shared_jobs[1]["id"],
        shared_jobs[2]["id"],
        reserved_job["id"],
    ]
    database.set_job_status(reserved_job["id"], "completed")
    # The reserved slot stays idle rather than being lent to the shared queue.
    assert database.claim_next_job(4) is None
    assert database.get_job(shared_jobs[3]["id"])["status"] == "queued"
    database.close()


def test_reserved_queue_uses_shared_pool_up_to_its_worker_limit(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "reserved-shared.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    reserved = database.create_work_queue("Резерв", 0, 2, reserved_workers=1)
    shared = database.create_work_queue("Общая", 100, 4)
    shared_jobs = [
        _job(database, user["id"], f"shared-{index}", f"shared-{index}",
             str(workspace), queue_id=shared["id"])
        for index in range(4)
    ]
    reserved_jobs = [
        _job(database, user["id"], f"reserved-{index}", f"reserved-{index}",
             str(workspace), queue_id=reserved["id"])
        for index in range(2)
    ]

    claimed = []
    while job := database.claim_next_job(6):
        claimed.append(job["id"])

    # Six workers: four shared jobs, one guaranteed reserved slot, then the
    # second reserved-queue job from the shared pool.
    assert claimed == [
        shared_jobs[0]["id"],
        shared_jobs[1]["id"],
        shared_jobs[2]["id"],
        shared_jobs[3]["id"],
        reserved_jobs[0]["id"],
        reserved_jobs[1]["id"],
    ]
    assert database.claim_next_job(6) is None
    database.close()


def test_reserved_workers_validation_releases_on_archive(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "reserved-validation.sqlite", "x" * 32)
    database.initialize()
    with pytest.raises(ValueError, match="cannot exceed its worker limit"):
        database.create_work_queue("Bad", 0, 1, reserved_workers=2)

    first = database.create_work_queue("First", 0, 2, reserved_workers=1, limit=2)
    # Omitting reserved_workers on update preserves the current reservation.
    updated = database.update_work_queue(
        first["id"], name="First", priority=0, max_workers=2, limit=1
    )
    assert updated and updated["reserved_workers"] == 1
    with pytest.raises(ValueError, match="active session limit"):
        database.create_work_queue("Second", 0, 2, reserved_workers=2, limit=2)
    with pytest.raises(ValueError, match="active session limit"):
        database.update_work_queue(
            first["id"], name="First", priority=0, max_workers=2,
            reserved_workers=2, limit=1,
        )

    assert database.archive_work_queue(first["id"])
    assert database.reserved_workers_total() == 0
    database.create_work_queue("Replacement", 0, 2, reserved_workers=1, limit=1)
    database.close()


def test_initialize_adds_reserved_workers_to_legacy_queue_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy-queues.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE work_queues (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            priority INTEGER NOT NULL DEFAULT 0,
            max_workers INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            is_system INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.commit()
    connection.close()

    database = Database(path, "x" * 32)
    database.initialize()
    assert database.get_work_queue(DEFAULT_QUEUE_ID)["reserved_workers"] == 0
    database.close()


def test_deployment_continuation_regains_a_slot_before_new_high_priority_work(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "recovery-priority.sqlite", "x" * 32)
    database.initialize()
    user = database.create_user("user", "long-enough-password", "user", str(workspace))
    urgent = database.create_work_queue("Срочные", 100, 1)
    background = database.create_work_queue("Фоновые", 10, 1)
    recovered = database.create_job(
        chat=database.create_chat(user["id"], "recovered", "Recovered", str(workspace)),
        effective_user_id=user["id"], actor_user_id=user["id"], content="original",
        agent="build", provider_id=None, model_id=None, model_variant=None,
        queue_id=background["id"],
    )
    claimed = database.claim_next_job(1)
    database.set_job_status(claimed["id"], "running")
    assert database.resume_interrupted_job(
        recovered["id"], recovered["message_id"], "continue safely"
    )
    fresh = database.create_job(
        chat=database.create_chat(user["id"], "fresh", "Fresh", str(workspace)),
        effective_user_id=user["id"], actor_user_id=user["id"], content="new urgent work",
        agent="build", provider_id=None, model_id=None, model_variant=None,
        queue_id=urgent["id"],
    )

    assert database.claim_next_job(1)["id"] == recovered["id"]
    database.set_job_status(recovered["id"], "completed")
    assert database.claim_next_job(1)["id"] == fresh["id"]
    database.close()


def test_archiving_queue_requires_removing_live_assignments(
    tmp_path: Path, workspace: Path
) -> None:
    database = Database(tmp_path / "archive-queue.sqlite", "x" * 32)
    database.initialize()
    admin = database.create_user("admin", "long-enough-password", "admin", None)
    queue = database.create_work_queue("Temporary", 1, 1)
    database.set_queue_setting("default_chat_queue_id", queue["id"], admin["id"])
    with pytest.raises(ValueError, match="assigned"):
        database.archive_work_queue(queue["id"])
    database.set_queue_setting("default_chat_queue_id", "default", admin["id"])
    assert database.archive_work_queue(queue["id"])
    assert database.get_work_queue(queue["id"])["status"] == "archived"
    database.close()
