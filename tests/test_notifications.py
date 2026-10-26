from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from test_task_database import start_task
from test_task_database import task_database as task_database
from test_task_pipeline import pipeline

from opencode_web.database import Database
from opencode_web.notifications import (
    MattermostNotificationError,
    MattermostNotifier,
)

MM_USER = "a" * 26


def test_existing_database_migrates_mattermost_and_outbox(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE users (
               id TEXT PRIMARY KEY, login TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
               role TEXT NOT NULL, tasks_only INTEGER NOT NULL DEFAULT 0,
               workspace_path TEXT, status TEXT NOT NULL DEFAULT 'active',
               auth_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL, last_login_at TEXT
           )"""
    )
    connection.commit()
    connection.close()
    db = Database(path, "secret")
    try:
        db.initialize()
        columns = {row[1] for row in db._connection.execute("PRAGMA table_info(users)")}
        assert "mattermost_username" in columns
        assert "mattermost_user_id" in columns
        assert db._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'notification_outbox'"
        ).fetchone()
    finally:
        db.close()


def test_mattermost_identity_validation_uniqueness_and_clearing(task_database):
    db, _, user, _ = task_database
    updated = db.update_user(
        user["id"], mattermost_username="  @Akhamitsevich  ",
        mattermost_user_id=f"  {MM_USER}  ",
    )
    assert updated["mattermost_username"] == "akhamitsevich"
    assert updated["mattermost_user_id"] == MM_USER
    assert db.list_users()[1]["mattermost_username"] == "akhamitsevich"
    assert db.list_users()[1]["mattermost_user_id"] == MM_USER
    with pytest.raises(ValueError, match="start with a letter"):
        db.update_user(user["id"], mattermost_username="12")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.create_user(
            "other", "long-enough-password", "user", user["workspace_path"] + "/other",
            mattermost_username="akhamitsevich",
            mattermost_user_id=MM_USER,
        )
    cleared = db.update_user(user["id"], mattermost_username="")
    assert cleared["mattermost_username"] is None
    assert cleared["mattermost_user_id"] is None


def test_task_completion_creates_one_durable_notification(task_database):
    db, _, user, task = task_database
    db.update_user(user["id"], mattermost_user_id=MM_USER)
    run = start_task(db, db.get_user(user["id"]), task, "session")
    db.complete_task_run(run["id"], {"text": "private result"})
    db.complete_task_run(run["id"], {"text": "duplicate"})
    (notification,) = db.list_notifications()
    assert notification["kind"] == "task_completed"
    assert notification["object_id"] == run["id"]
    assert notification["mattermost_user_id"] == MM_USER
    assert notification["link_path"] == f"/tasks/runs/{run['id']}"
    assert "private result" not in str(notification)

    reopened = Database(db.path, db.session_secret)
    try:
        reopened.initialize()
        assert len(reopened.list_notifications()) == 1
    finally:
        reopened.close()


def test_no_mattermost_id_means_no_notification(task_database):
    db, _, user, task = task_database
    run = start_task(db, user, task, "session")
    db.complete_task_run(run["id"], {"text": "Done"})
    assert db.list_notifications() == []


def test_pipeline_notifies_once_only_after_every_branch_finishes(task_database):
    db, admin, user, root = task_database
    db.update_user(user["id"], mattermost_user_id=MM_USER)
    root, b, c, d = pipeline(db, admin, root)
    run = start_task(db, db.get_user(user["id"]), root, "root")
    db.complete_task_run(run["id"], None)
    assert db.list_notifications() == []
    children = db.pipeline_children(run["id"])
    b_run = next(item for item in children if item["task_id"] == b["id"])
    c_run = next(item for item in children if item["task_id"] == c["id"])
    db.complete_task_run(c_run["id"], None)
    db.complete_task_run(b_run["id"], None)
    assert db.list_notifications() == []
    (d_run,) = db.pipeline_children(b_run["id"])
    assert d_run["task_id"] == d["id"]
    db.complete_task_run(d_run["id"], None)
    (notification,) = db.list_notifications()
    assert notification["object_id"] == run["id"]
    assert notification["title"] == root["title"]


def test_failed_pipeline_and_noncompleted_jobs_do_not_notify(task_database):
    db, admin, user, root = task_database
    db.update_user(user["id"], mattermost_user_id=MM_USER)
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, db.get_user(user["id"]), root, "root")
    db.set_job_status(run["id"], "failed", "test")
    db.complete_task_run(run["id"], None)
    assert db.list_notifications() == []


def test_consultation_completion_and_notification_are_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite", "secret")
    db.initialize()
    user = db.create_user(
        "worker", "long-enough-password", "user", str(tmp_path),
        mattermost_user_id=MM_USER,
    )
    chat = db.create_chat(user["id"], "session", "Архитектура", str(tmp_path), consultation=True)
    job = db.create_job(
        chat=chat, effective_user_id=user["id"], actor_user_id=user["id"],
        content="Вопрос", agent="plan", provider_id="p", model_id="m", model_variant=None,
    )
    try:
        assert db.complete_consultation_run(job["id"], "Ответ")
        assert not db.complete_consultation_run(job["id"], "Дубликат")
        assert db.consultation_jobs(chat["id"])[0]["answer"] == "Ответ"
        (notification,) = db.list_notifications()
        assert notification["kind"] == "consultation_answer_ready"
        assert notification["link_path"] == f"/questions/{chat['id']}"
    finally:
        db.close()


def consultation_case(tmp_path):
    db = Database(tmp_path / "db.sqlite", "secret")
    db.initialize()
    user = db.create_user(
        "worker", "long-enough-password", "user", str(tmp_path),
        mattermost_user_id=MM_USER,
    )

    def ask(chat=None, title="Вопрос"):
        chat = chat or db.create_chat(user["id"], None, title, str(tmp_path), consultation=True)
        job = db.create_job(
            chat=chat, effective_user_id=user["id"], actor_user_id=user["id"],
            content=title, agent="plan", provider_id="p", model_id="m", model_variant=None,
        )
        return chat, job

    return db, ask


def test_deleting_a_chat_drops_its_unsent_notifications(tmp_path):
    db, ask = consultation_case(tmp_path)
    try:
        gone, first = ask(title="Удалю")
        assert db.complete_consultation_run(first["id"], "Ответ")
        sent = db.claim_notification()
        db.mark_notification_sent(sent["id"], "post")
        _, second = ask(gone)
        assert db.complete_consultation_run(second["id"], "Ещё ответ")
        kept, other = ask(title="Оставлю")
        assert db.complete_consultation_run(other["id"], "Ответ")
        assert len(db.list_notifications()) == 3

        assert db.delete_chat(gone["id"])
        remaining = {(row["object_id"], row["status"]) for row in db.list_notifications()}
        assert remaining == {(gone["id"], "sent"), (kept["id"], "pending")}

        # An answer finishing after the delete does not notify either.
        _, late = ask(kept)
        assert db.delete_chat(kept["id"])
        assert db.complete_consultation_run(late["id"], "Поздно")
        assert [row["status"] for row in db.list_notifications()] == ["sent"]
    finally:
        db.close()


def test_deleting_a_task_run_drops_its_notification(task_database):
    db, _, user, task = task_database
    db.update_user(user["id"], mattermost_user_id=MM_USER)
    worker = db.get_user(user["id"])
    run = start_task(db, worker, task, "first")
    db.complete_task_run(run["id"], {"text": "Готово"})
    assert len(db.list_notifications()) == 1
    assert db.delete_chat(db.get_job(run["id"])["chat_id"])
    assert db.list_notifications() == []

    late = start_task(db, worker, task, "second")
    assert db.delete_chat(db.get_job(late["id"])["chat_id"])
    db.complete_task_run(late["id"], {"text": "Готово"})
    assert db.list_notifications() == []


def test_outbox_retry_and_recovery(task_database):
    db, _, user, task = task_database
    db.update_user(user["id"], mattermost_user_id=MM_USER)
    run = start_task(db, db.get_user(user["id"]), task, "session")
    db.complete_task_run(run["id"], None)
    claimed = db.claim_notification()
    assert claimed["status"] == "sending" and claimed["attempts"] == 1
    reopened = Database(db.path, db.session_secret)
    try:
        reopened.initialize()
        assert reopened.list_notifications()[0]["status"] == "pending"
        claimed = reopened.claim_notification()
        reopened.mark_notification_failed(
            claimed["id"], "http_404", permanent=True, max_attempts=8
        )
        assert reopened.list_notifications()[0]["status"] == "dead"
    finally:
        reopened.close()


async def test_notifier_creates_dm_and_posts_only_link():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "bot"})
        if request.url.path == "/api/v4/channels/direct":
            return httpx.Response(201, json={"id": "dm"})
        return httpx.Response(201, json={"id": "post"})

    notifier = MattermostNotifier.__new__(MattermostNotifier)
    notifier.timeout = 5
    notifier._bot_id = None
    notifier.client = httpx.AsyncClient(
        base_url="https://mm.example", transport=httpx.MockTransport(handler)
    )
    notification = {
        "id": "notification",
        "kind": "task_completed",
        "title": "Проверка",
        "mattermost_user_id": MM_USER,
        "link_path": "/#/tasks/runs/run",
    }
    try:
        assert await notifier.send(notification, "http://valeria.example/") == "post"
    finally:
        await notifier.close()
    assert requests[1].read().decode() == f'["bot","{MM_USER}"]'
    post = json.loads(requests[2].read())
    assert post["channel_id"] == "dm"
    assert post["pending_post_id"] == "valeria-notification:notification"
    assert "http://valeria.example/#/tasks/runs/run" in post["message"]


async def test_notifier_resolves_username_to_canonical_identity():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": MM_USER, "username": "akhamitsevich"})

    notifier = MattermostNotifier.__new__(MattermostNotifier)
    notifier.timeout = 5
    notifier._bot_id = None
    notifier.client = httpx.AsyncClient(
        base_url="https://mm.example", transport=httpx.MockTransport(handler)
    )
    try:
        assert await notifier.user_by_username("akhamitsevich") == {
            "id": MM_USER, "username": "akhamitsevich",
        }
    finally:
        await notifier.close()
    assert requests[0].url.path == "/api/v4/users/username/akhamitsevich"


async def test_notifier_classifies_safe_errors():
    notifier = MattermostNotifier.__new__(MattermostNotifier)
    notifier.timeout = 5
    notifier._bot_id = "bot"
    notifier.client = httpx.AsyncClient(
        base_url="https://mm.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "17"}, text="secret")
        ),
    )
    try:
        with pytest.raises(MattermostNotificationError) as raised:
            await notifier._request("POST", "/api/v4/posts", json={})
    finally:
        await notifier.close()
    assert raised.value.code == "http_429"
    assert raised.value.retry_after_seconds == 17
    assert "secret" not in str(raised.value)
