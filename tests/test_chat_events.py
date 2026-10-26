from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_chat_actions import KIND_CASES, MAKERS, future, make_folder, make_question, make_run
from test_chat_actions import ui_app as ui_app
from test_consultations import dispatcher_case as dispatcher_case
from test_tasks_api import login

from opencode_web.app import event_for_user
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.opencode_client import OpenCodeError

CHAT_FIELDS = {
    "type", "kind", "id", "chat_id", "title", "status", "phase", "task_id", "created_at",
    "updated_at", "archived_at", "snoozed_until",
}


class Recorder:
    def __init__(self, broker=None):
        self.events: list[dict] = []
        self.seen = asyncio.Event()
        self._original = broker.publish if broker else None

    async def publish(self, event):
        self.events.append(event)
        if event.get("type", "").startswith("chat."):
            self.seen.set()
        if self._original:
            await self._original(event)

    def of(self, *types):
        return [event for event in self.events if event.get("type") in types]


def record(t, monkeypatch):
    recorder = Recorder(t.app.state.events)
    monkeypatch.setattr(t.app.state.events, "publish", recorder.publish)
    return recorder


def views(t, event):
    return {name: event_for_user(t.db, user, event) for name, user in t.users.items()}


def attach_session(t, chat_id, session_id, title):
    with t.db.transaction() as connection:
        connection.execute(
            "UPDATE queue_jobs SET session_id = ?, session_pending = 0 WHERE chat_id = ?",
            (session_id, chat_id),
        )
        connection.execute(
            "UPDATE chats SET opencode_session_id = ? WHERE id = ?", (session_id, chat_id)
        )
    t.manager.client.session_records[session_id] = {"id": session_id, "title": title}


@pytest.mark.parametrize(("username", "kind"), KIND_CASES)
async def test_chat_actions_publish_events_only_for_owner_and_admins(
    ui_app, monkeypatch, username, kind
):
    t = ui_app
    made = await MAKERS[kind](t, username)
    chat_id = made["chat_id"]
    recorder = record(t, monkeypatch)
    for action, body in (
        ("archive", None), ("archive", None), ("unarchive", None),
        ("snooze", {"until": future()}), ("unsnooze", None), ("unsnooze", None),
    ):
        response = await t.client.post(f"/api/chats/{chat_id}/{action}", json=body)
        assert response.status_code == 200, response.text
    assert (await t.client.delete(f"/api/chats/{chat_id}")).status_code == 204

    # Idempotent repeats change nothing, so they publish nothing. Deleting a folder
    # chat first stops its queued job, and that end updates the chat once more.
    published = recorder.of("chat.updated", "chat.deleted")
    stopped = ["chat.updated"] if kind == "folder" else []
    assert [event["type"] for event in published] == (
        ["chat.updated"] * 4 + stopped + ["chat.deleted"]
    )
    states = [
        (bool(event["item"]["archived_at"]), bool(event["item"]["snoozed_until"]))
        for event in published[:4]
    ]
    assert states == [(True, False), (False, False), (False, True), (False, False)]

    archived = views(t, published[0])
    owner = archived[username]
    assert set(owner) == CHAT_FIELDS
    assert (owner["type"], owner["chat_id"], owner["id"], owner["kind"]) == (
        "chat.updated", chat_id, made["id"], kind,
    )
    assert owner["archived_at"] and owner["status"] == "queued"
    assert archived["admin"] == {**owner, "owner_user_id": t.users[username]["id"]}
    others = {"worker", "tasker", "stranger"} - {username}
    assert all(archived[name] is None for name in others)

    deleted = views(t, published[-1])
    assert deleted[username] == {"type": "chat.deleted", "chat_id": chat_id}
    assert deleted["admin"] == {
        "type": "chat.deleted", "chat_id": chat_id, "owner_user_id": t.users[username]["id"],
    }
    assert all(deleted[name] is None for name in others)
    # After deletion a late update for the chat reaches admins only.
    late = views(t, {**published[0], "item": published[-1]["item"]})
    assert late[username] is None and late["admin"]["chat_id"] == chat_id


async def test_chat_events_follow_item_visibility_rules(ui_app):
    t = ui_app
    folder = await make_folder(t, "worker")
    question = await make_question(t, "tasker")
    row = t.db.list_items(chat_id=folder["chat_id"])[0]
    event = {"type": "chat.updated", "item": row}
    assert event_for_user(t.db, t.users["worker"], event)["chat_id"] == folder["chat_id"]

    # tasks_only accounts never see folder chats, even their own legacy ones.
    tasker_folder = {**row, "owner_user_id": t.users["tasker"]["id"]}
    assert event_for_user(t.db, t.users["tasker"], {**event, "item": tasker_folder}) is None
    # A chat left behind in an old workspace is no longer the user's.
    moved = {**row, "workspace_snapshot": "/elsewhere"}
    assert event_for_user(t.db, t.users["worker"], {**event, "item": moved}) is None
    assert event_for_user(t.db, t.users["admin"], {**event, "item": moved}) is not None

    own_question = t.db.list_items(chat_id=question["chat_id"])[0]
    visible = event_for_user(t.db, t.users["tasker"], {**event, "item": own_question})
    assert visible["kind"] == "question" and visible["phase"] is None


async def test_title_refresh_and_sync_publish_title_events(ui_app, monkeypatch):
    t = ui_app
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    attach_session(t, folder["chat_id"], "folder-session", "Разбор логов")
    attach_session(t, run["chat_id"], "task-session", "Сборка релиза")
    recorder = record(t, monkeypatch)

    detail = await t.client.get(f"/api/chats/{folder['chat_id']}")
    assert detail.status_code == 200, detail.text
    [chat_event] = recorder.of("chat.updated")
    assert views(t, chat_event)["worker"]["title"] == "Разбор логов"
    assert recorder.of("task.title.updated") == []
    # Unchanged titles publish nothing.
    await t.client.get(f"/api/chats/{folder['chat_id']}")
    assert len(recorder.of("chat.updated")) == 1

    synced = await t.client.post("/api/task-runs/title-sync")
    assert synced.status_code == 200, synced.text
    expected = f"{t.task['title']}: Сборка релиза"
    assert synced.json() == [{"id": run["id"], "title": expected}]
    [title_event] = recorder.of("task.title.updated")
    seen = views(t, title_event)
    assert seen["worker"] == {
        "type": "task.title.updated", "run_id": run["id"], "chat_id": run["chat_id"],
        "title": expected, "task": {"id": run["id"], "title": expected},
    }
    assert seen["admin"]["owner_user_id"] == t.users["worker"]["id"]
    assert seen["tasker"] is None and seen["stranger"] is None
    task_update = recorder.of("chat.updated")[-1]
    assert views(t, task_update)["worker"]["title"] == expected


async def test_live_session_title_reaches_folder_and_task_chats(ui_app, monkeypatch):
    t = ui_app
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    question = await make_question(t, "worker", "Исходный вопрос")
    attach_session(t, folder["chat_id"], "folder-live", "Новый диалог")
    attach_session(t, run["chat_id"], "task-live", "start")
    question_session = t.db.get_chat(question["chat_id"])["opencode_session_id"]
    workspace = t.users["worker"]["workspace_path"]
    recorder = record(t, monkeypatch)

    def session_updated(session_id, title):
        return {
            "type": "opencode.event",
            "workspace": workspace,
            "event": {"payload": {
                "type": "session.updated",
                "properties": {"info": {"id": session_id, "title": title}},
            }},
        }

    await t.publish_opencode(session_updated("folder-live", "Живой заголовок"))
    await t.publish_opencode(session_updated("task-live", "Живая задача"))
    await t.publish_opencode(session_updated(question_session, "Чужой заголовок"))
    await t.publish_opencode(session_updated("unknown-session", "Ничей"))
    await t.publish_opencode({"type": "opencode.event", "workspace": workspace, "event": {}})

    assert t.db.get_chat(folder["chat_id"])["title"] == "Живой заголовок"
    assert t.db.get_chat(run["chat_id"])["title"] == "Живая задача"
    # Consultations keep the title made from the question.
    assert t.db.get_chat(question["chat_id"])["title"] == "Исходный вопрос"
    assert [event["item"]["chat_id"] for event in recorder.of("chat.updated")] == [
        folder["chat_id"], run["chat_id"],
    ]
    [title_event] = recorder.of("task.title.updated")
    assert views(t, title_event)["worker"]["title"] == f"{t.task['title']}: Живая задача"
    assert len(recorder.of("opencode.event")) == 5

    def broken(*args):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(t.db, "chat_for_session", broken)
    await t.publish_opencode(session_updated("folder-live", "Ещё раз"))
    assert len(recorder.of("opencode.event")) == 6


async def test_workspace_change_archive_publishes_chat_updates(ui_app, monkeypatch, workspace):
    t = ui_app
    folder = await make_folder(t, "worker")
    for job in t.db.unfinished_chat_jobs(folder["chat_id"]):
        t.db.set_job_status(job["id"], "cancelled")
    recorder = record(t, monkeypatch)
    await login(t.client)
    new_path = workspace / "worker-next"
    new_path.mkdir()
    response = await t.client.patch(
        f"/api/admin/users/{t.users['worker']['id']}", json={"workspace_path": str(new_path)}
    )
    assert response.status_code == 200, response.text
    [event] = recorder.of("chat.updated")
    assert event["item"]["chat_id"] == folder["chat_id"] and event["item"]["archived_at"]
    assert views(t, event)["admin"]["archived_at"]


def folder_monitor_client(session_id, *, questions=(), permissions=()):
    return SimpleNamespace(
        session_statuses=AsyncMock(return_value={session_id: {"type": "busy"}}),
        questions=AsyncMock(return_value=[dict(item) for item in questions]),
        permissions=AsyncMock(return_value=[dict(item) for item in permissions]),
    )


@pytest.mark.parametrize("reason", ["permission", "question"])
async def test_folder_chat_that_needs_the_user_ends_its_snooze(ui_app, monkeypatch, reason):
    t = ui_app
    folder = await make_folder(t, "worker")
    chat_id = folder["chat_id"]
    await t.client.post(f"/api/chats/{chat_id}/snooze", json={"until": future()})
    job = t.db.unfinished_chat_jobs(chat_id)[0]
    assert t.db.claim_next_job(5)["id"] == job["id"]
    attach_session(t, chat_id, "folder-monitor", "Новый диалог")
    t.db.set_job_status(job["id"], "running")
    request = {"id": "r1", "sessionID": "folder-monitor"}
    client = folder_monitor_client(
        "folder-monitor",
        questions=[{**request, "questions": [{"question": "?"}]}] if reason == "question" else (),
        permissions=[request] if reason == "permission" else (),
    )
    recorder = record(t, monkeypatch)
    dispatcher = QueueDispatcher(t.db, t.manager, t.app.state.events, 1)
    worker = asyncio.create_task(dispatcher._monitor(t.db.get_job(job["id"]), client))
    try:
        await asyncio.wait_for(recorder.seen.wait(), 2)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    assert t.db.get_chat(chat_id)["snoozed_until"] is None
    [event] = recorder.of("chat.updated")
    assert views(t, event)["worker"]["snoozed_until"] is None
    assert t.db.get_job(job["id"])["status"] == f"waiting_{reason}"


async def my_item_status(t, chat_id):
    response = await t.client.get("/api/me/items")
    assert response.status_code == 200, response.text
    [item] = [item for item in response.json() if item["chat_id"] == chat_id]
    return item["status"]


@pytest.mark.parametrize("reason", ["permission", "question"])
async def test_folder_chat_status_shows_when_it_waits_for_the_user(ui_app, monkeypatch, reason):
    t = ui_app
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_sleep)
    folder = await make_folder(t, "worker")
    chat_id = folder["chat_id"]
    job = t.db.unfinished_chat_jobs(chat_id)[0]
    assert t.db.claim_next_job(5)["id"] == job["id"]
    attach_session(t, chat_id, "folder-monitor", "Новый диалог")
    t.db.set_job_status(job["id"], "running")
    request = {"id": "r1", "sessionID": "folder-monitor"}
    client = folder_monitor_client(
        "folder-monitor",
        questions=[{**request, "questions": [{"question": "?"}]}] if reason == "question" else (),
        permissions=[request] if reason == "permission" else (),
    )
    recorder = record(t, monkeypatch)

    async def chat_updates(count):
        while len(recorder.of("chat.updated")) < count:
            await original_sleep(0)
        return recorder.of("chat.updated")

    dispatcher = QueueDispatcher(t.db, t.manager, t.app.state.events, 1)
    worker = asyncio.create_task(dispatcher._monitor(t.db.get_job(job["id"]), client))
    try:
        [waiting] = await asyncio.wait_for(chat_updates(1), 2)
        assert views(t, waiting)["worker"]["status"] == f"waiting_{reason}"
        assert await my_item_status(t, chat_id) == f"waiting_{reason}"

        # The user answers; the agent carries on.
        client.questions.return_value = []
        client.permissions.return_value = []
        [_, resumed] = await asyncio.wait_for(chat_updates(2), 2)
        assert views(t, resumed)["worker"]["status"] == "running"
        assert await my_item_status(t, chat_id) == "running"
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    assert len(recorder.of("chat.updated")) == 2


async def started_folder(t):
    folder = await make_folder(t, "worker")
    chat_id = folder["chat_id"]
    job = t.db.unfinished_chat_jobs(chat_id)[0]
    assert t.db.claim_next_job(5)["id"] == job["id"]
    attach_session(t, chat_id, "folder-monitor", "Новый диалог")
    return chat_id, t.db.get_job(job["id"])


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_folder_chat_end_publishes_one_chat_update(ui_app, monkeypatch, outcome):
    t = ui_app
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_sleep)
    chat_id, job = await started_folder(t)
    # A snoozed chat: its question clears the snooze along with the waiting status.
    await t.client.post(f"/api/chats/{chat_id}/snooze", json={"until": future()})
    question = {"id": "r1", "sessionID": "folder-monitor", "questions": [{"question": "?"}]}
    client = folder_monitor_client("folder-monitor", questions=[question])
    recorder = record(t, monkeypatch)
    dispatcher = QueueDispatcher(t.db, t.manager, t.app.state.events, 1)
    worker = asyncio.create_task(dispatcher._execute(job, client))

    async def chat_updates(count):
        while len(recorder.of("chat.updated")) < count:
            await original_sleep(0)
        return recorder.of("chat.updated")

    try:
        [waiting] = await asyncio.wait_for(chat_updates(1), 2)
        assert views(t, waiting)["worker"]["status"] == "waiting_question"
        assert views(t, waiting)["worker"]["snoozed_until"] is None
        # The user answers; then the agent either finishes or OpenCode goes away.
        client.questions.return_value = []
        if outcome == "completed":
            client.session_statuses.return_value = {}
        else:
            client.session_statuses.side_effect = OpenCodeError("unavailable")
        await asyncio.wait_for(worker, 2)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    assert t.db.get_job(job["id"])["status"] == outcome
    updates = recorder.of("chat.updated")
    ended = updates[-1]
    assert ended["item"]["job_status"] == outcome
    assert set(views(t, ended)["worker"]) == CHAT_FIELDS
    assert views(t, ended)["worker"]["status"] == await my_item_status(t, chat_id)
    assert views(t, ended)["worker"]["status"] != "running"
    expected = ["waiting_question", "running", outcome] if outcome == "completed" else [
        "waiting_question", outcome,
    ]
    assert [event["item"]["job_status"] for event in updates] == expected
    assert recorder.of("job.updated")[-1]["job"]["status"] == outcome


async def test_stopping_folder_chat_publishes_one_chat_update(ui_app, monkeypatch):
    t = ui_app
    queued = await make_folder(t, "worker")
    queued_job = t.db.unfinished_chat_jobs(queued["chat_id"])[0]
    recorder = record(t, monkeypatch)
    dispatcher = QueueDispatcher(t.db, t.manager, t.app.state.events, 1)
    await dispatcher.stop_job(queued_job["id"])
    [event] = recorder.of("chat.updated")
    assert event["item"]["chat_id"] == queued["chat_id"]
    assert event["item"]["job_status"] == "cancelled"

    # A running job ends once OpenCode confirms the abort.
    chat_id, job = await started_folder(t)
    t.db.set_job_status(job["id"], "running")
    t.manager.client.abort = AsyncMock(return_value=True)
    t.manager.client.session_statuses = AsyncMock(return_value={})
    await dispatcher.stop_job(job["id"])
    await asyncio.wait_for(dispatcher._cancellations[job["id"]], 2)
    assert t.db.get_job(job["id"])["status"] == "cancelled"
    [_, event] = recorder.of("chat.updated")
    assert event["item"]["chat_id"] == chat_id
    assert event["item"]["job_status"] == "cancelled"
    # Stopping a finished job again publishes nothing more.
    await dispatcher.stop_job(job["id"])
    assert len(recorder.of("chat.updated")) == 2


async def test_consultation_question_ends_snooze_live_and_after_restart(dispatcher_case):
    db, runner, client, job = dispatcher_case
    recorder = Recorder()
    runner.events = recorder
    client.pending_questions = [{
        "id": "q", "sessionID": job["session_id"],
        "questions": [{"question": "Продолжить?", "options": [{"label": "Да"}]}],
    }]
    assert db.snooze_chat(job["chat_id"], future())
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "waiting_question"
    assert db.get_chat(job["chat_id"])["snoozed_until"] is None
    [event] = recorder.of("chat.updated")
    assert event["item"]["chat_id"] == job["chat_id"]
    assert event["item"]["job_status"] == "waiting_question"

    # Recovery re-reads the pending question and resurfaces a chat snoozed meanwhile.
    assert db.snooze_chat(job["chat_id"], future())
    await runner.recover()
    assert db.get_chat(job["chat_id"])["snoozed_until"] is None
    assert len(recorder.of("chat.updated")) == 2
