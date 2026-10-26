from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_admin_stop import make_dispatcher, settle
from test_consultations import ConsultationClient
from test_tasks_api import create_task, login

from opencode_web.app import create_app, event_for_user
from opencode_web.database import Database


class UiManager:
    def __init__(self):
        self.client = ConsultationClient()

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True):
        yield self.client

    async def ensure(self, workspace):
        return self.client

    def instances(self):
        return []

    async def close(self):
        pass


@pytest.fixture
async def ui_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        publish_opencode = app.state.manager.event_sink
        manager = UiManager()
        app.state.manager = manager
        db = app.state.database
        users = {}
        for name, only in (("worker", False), ("tasker", True), ("stranger", False)):
            path = workspace / name
            path.mkdir()
            users[name] = db.create_user(
                name, "a-strong-password", "user", str(path), tasks_only=only
            )
        users["admin"] = db.get_user_by_login("admin")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            t = SimpleNamespace(
                app=app, client=client, db=db, users=users, manager=manager,
                publish_opencode=publish_opencode,
            )
            await login(client)
            response = await client.put(
                "/api/admin/settings/consultations", json={"model": "test/coder"}
            )
            assert response.status_code == 200, response.text
            t.task = await create_task(client)
            yield t


async def make_question(t, name, message="Как настроить CI?"):
    await login(t.client, name)
    response = await t.client.post("/api/consultations", json={"message": message})
    assert response.status_code == 201, response.text
    chat_id = response.json()["id"]
    return {"chat_id": chat_id, "id": chat_id, "kind": "question"}


async def make_folder(t, name, message="Запусти тесты"):
    await login(t.client, name)
    response = await t.client.post("/api/chats", json={"message": message})
    assert response.status_code == 201, response.text
    chat_id = response.json()["chat"]["id"]
    return {"chat_id": chat_id, "id": chat_id, "kind": "folder"}


async def make_run(t, name):
    await login(t.client, name)
    response = await t.client.post(f"/api/tasks/{t.task['id']}/runs", json={})
    assert response.status_code == 202, response.text
    run = response.json()
    return {"chat_id": t.db.get_job(run["id"])["chat_id"], "id": run["id"], "kind": "task"}


MAKERS = {"question": make_question, "folder": make_folder, "task": make_run}
KIND_CASES = [
    ("worker", "question"), ("worker", "folder"), ("worker", "task"),
    ("tasker", "question"), ("tasker", "task"),
]


def future(hours=1):
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def audit_actions(db, chat_id):
    return [
        entry["action"] for entry in reversed(db.list_audit())
        if entry["object_type"] == "chat" and entry["object_id"] == chat_id
    ]


async def user_lists(t):
    lists = [(await t.client.get("/api/consultations")).json()]
    lists.append((await t.client.get("/api/task-runs")).json())
    return {entry["id"] for entries in lists for entry in entries}


@pytest.mark.parametrize(("username", "kind"), KIND_CASES)
async def test_archive_snooze_and_delete_for_every_chat_kind(ui_app, username, kind):
    t = ui_app
    made = await MAKERS[kind](t, username)
    chat_id = made["chat_id"]
    base = f"/api/chats/{chat_id}"

    archived = await t.client.post(f"{base}/archive")
    assert archived.status_code == 200, archived.text
    item = archived.json()
    assert set(item) == {
        "kind", "id", "chat_id", "title", "status", "phase", "task_id", "created_at",
        "updated_at", "archived_at", "snoozed_until",
    }
    assert (item["kind"], item["id"], item["chat_id"]) == (kind, made["id"], chat_id)
    assert item["status"] == "queued"
    assert item["archived_at"] and item["snoozed_until"] is None
    assert item["task_id"] == (t.task["id"] if kind == "task" else None)
    assert item["phase"] == ("build" if kind == "task" else None)
    # Archiving keeps the work going.
    assert [job["status"] for job in t.db.unfinished_chat_jobs(chat_id)] == ["queued"]
    assert (await t.client.post(f"{base}/archive")).json()["archived_at"] == item["archived_at"]
    snoozed_archived = await t.client.post(f"{base}/snooze", json={"until": future()})
    assert snoozed_archived.status_code == 409

    restored = await t.client.post(f"{base}/unarchive")
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    until = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    snoozed = await t.client.post(
        f"{base}/snooze", json={"until": until.astimezone().isoformat()}
    )
    assert snoozed.status_code == 200, snoozed.text
    assert datetime.fromisoformat(snoozed.json()["snoozed_until"]) == until
    assert snoozed.json()["snoozed_until"].endswith("+00:00")
    # Archiving a snoozed chat clears the snooze.
    archived_snoozed = (await t.client.post(f"{base}/archive")).json()
    assert archived_snoozed["archived_at"] and archived_snoozed["snoozed_until"] is None
    await t.client.post(f"{base}/unarchive")
    await t.client.post(f"{base}/snooze", json={"until": future()})
    unsnoozed = await t.client.post(f"{base}/unsnooze")
    assert unsnoozed.status_code == 200 and unsnoozed.json()["snoozed_until"] is None

    if kind != "folder":
        assert made["id"] in await user_lists(t)
    deleted = await t.client.delete(base)
    assert deleted.status_code == 204 and deleted.content == b""
    assert t.db.get_chat(chat_id)["deleted_at"]
    assert t.db.unfinished_chat_jobs(chat_id) == []
    for action in ("archive", "unarchive", "unsnooze"):
        assert (await t.client.post(f"{base}/{action}")).status_code == 404
    assert (await t.client.delete(base)).status_code == 404
    assert made["id"] not in await user_lists(t)
    detail_path = {
        "question": f"/api/consultations/{chat_id}",
        "task": f"/api/task-runs/{made['id']}",
        "folder": f"/api/chats/{chat_id}",
    }[kind]
    assert (await t.client.get(detail_path)).status_code == 404
    if kind == "folder":
        assert (await t.client.get("/api/chats")).json() == []
        assert (await t.client.post(f"{base}/messages", json={"message": "x"})).status_code == 404
    assert audit_actions(t.db, chat_id) == [
        "chat.archive", "chat.unarchive", "chat.snooze", "chat.archive", "chat.unarchive",
        "chat.snooze", "chat.unsnooze", "chat.delete",
    ]
    entry = next(e for e in t.db.list_audit() if e["action"] == "chat.delete")
    assert entry["actor_user_id"] == entry["effective_user_id"] == t.users[username]["id"]

    # Administrators still see soft-deleted chats in their views.
    await login(t.client)
    owner_id = t.users[username]["id"]
    if kind == "question":
        listed = (await t.client.get("/api/admin/consultations")).json()
        assert chat_id in {entry["id"] for entry in listed}
        assert (await t.client.get(f"/api/admin/consultations/{chat_id}")).status_code == 200
    elif kind == "task":
        listed = (await t.client.get("/api/admin/task-runs")).json()
        assert made["id"] in {entry["id"] for entry in listed}
        listed = (await t.client.get(f"/api/admin/users/{owner_id}/task-runs")).json()
        assert made["id"] in {entry["id"] for entry in listed}
    else:
        listed = (await t.client.get(f"/api/admin/users/{owner_id}/chats")).json()
        assert chat_id in {entry["id"] for entry in listed}


async def test_chat_actions_are_owner_only_and_need_csrf(ui_app):
    t = ui_app
    made = [
        await make_question(t, "worker"), await make_folder(t, "worker"),
        await make_run(t, "worker"),
    ]
    for name in ("stranger", "admin"):
        await login(t.client, name)
        for entry in made:
            base = f"/api/chats/{entry['chat_id']}"
            for action in ("archive", "unarchive", "unsnooze"):
                assert (await t.client.post(f"{base}/{action}")).status_code == 404
            snooze = await t.client.post(f"{base}/snooze", json={"until": future()})
            assert snooze.status_code == 404
            assert (await t.client.delete(base)).status_code == 404
    await login(t.client, "worker")
    base = f"/api/chats/{made[0]['chat_id']}"
    csrf = t.client.headers.pop("x-csrf-token")
    assert (await t.client.post(f"{base}/archive")).status_code == 403
    assert (await t.client.delete(base)).status_code == 403
    t.client.headers["x-csrf-token"] = csrf
    t.client.cookies.clear()
    assert (await t.client.post(f"{base}/archive")).status_code == 401
    assert all(t.db.get_chat(entry["chat_id"])["archived_at"] is None for entry in made)
    assert all(t.db.get_chat(entry["chat_id"])["deleted_at"] is None for entry in made)


async def test_tasks_only_reaches_only_chat_actions(ui_app):
    t = ui_app
    tasker = t.users["tasker"]
    legacy = t.db.create_chat(tasker["id"], None, "Legacy", tasker["workspace_path"])
    question = await make_question(t, "tasker")
    base = f"/api/chats/{legacy['id']}"
    for action in ("archive", "unarchive", "unsnooze"):
        assert (await t.client.post(f"{base}/{action}")).status_code == 404
    assert (await t.client.post(f"{base}/snooze", json={"until": future()})).status_code == 404
    assert (await t.client.delete(base)).status_code == 404
    for method, path in (
        ("GET", base), ("GET", f"{base}/messages"), ("POST", f"{base}/abort"),
        ("GET", f"/api/chats/{question['chat_id']}"), ("GET", "/api/chats"),
        ("POST", "/api/chats/title-sync"),
    ):
        assert (await t.client.request(method, path)).status_code == 403, (method, path)
    archived = await t.client.post(f"/api/chats/{question['chat_id']}/archive")
    assert archived.status_code == 200


@pytest.mark.parametrize("until", [
    "2099-01-01T10:00:00",
    "not a date",
    None,
    (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
])
async def test_snooze_requires_future_time_with_offset(ui_app, until):
    t = ui_app
    made = await make_question(t, "worker")
    response = await t.client.post(f"/api/chats/{made['chat_id']}/snooze", json={"until": until})
    assert response.status_code == 422
    extra = await t.client.post(
        f"/api/chats/{made['chat_id']}/snooze", json={"until": future(), "note": "x"}
    )
    assert extra.status_code == 422
    assert t.db.get_chat(made["chat_id"])["snoozed_until"] is None
    assert "chat.snooze" not in audit_actions(t.db, made["chat_id"])


@pytest.mark.parametrize(("kind", "status"), [
    ("question", "waiting_question"), ("folder", "waiting_permission"),
])
async def test_waiting_for_the_user_clears_snooze(ui_app, kind, status):
    t = ui_app
    made = await MAKERS[kind](t, "worker")
    chat_id = made["chat_id"]
    assert (await t.client.post(f"/api/chats/{chat_id}/snooze", json={"until": future()})).json()[
        "snoozed_until"
    ]
    job = t.db.unfinished_chat_jobs(chat_id)[0]
    assert t.db.claim_next_job(5)["id"] == job["id"]
    assert t.db.set_job_status(job["id"], "running") is False
    assert t.db.get_chat(chat_id)["snoozed_until"]
    assert t.db.set_job_status(job["id"], status) is True
    assert t.db.get_chat(chat_id)["snoozed_until"] is None
    assert t.db.set_job_status(job["id"], status) is False


@pytest.mark.parametrize("kind", ["question", "folder", "task"])
@pytest.mark.parametrize("status", ["queued", "running", "waiting_question"])
async def test_deleting_unfinished_chat_stops_its_job_first(ui_app, kind, status):
    t = ui_app
    made = await MAKERS[kind](t, "worker")
    job = t.db.unfinished_chat_jobs(made["chat_id"])[0]
    if status != "queued":
        assert t.db.claim_next_job(5)["id"] == job["id"]
        with t.db.transaction() as connection:
            connection.execute(
                "UPDATE queue_jobs SET session_pending = 0, session_id = ? WHERE id = ?",
                (f"session-{job['id']}", job["id"]),
            )
        t.db.set_job_status(job["id"], status)
    dispatcher, client = make_dispatcher(t.db)
    t.app.state.dispatcher = dispatcher
    response = await t.client.delete(f"/api/chats/{made['chat_id']}")
    assert response.status_code == 204
    await settle(dispatcher)
    stopped = t.db.get_job(job["id"])
    assert stopped["status"] == "cancelled"
    assert t.db.active_jobs() == []
    if status == "queued":
        client.abort.assert_not_awaited()
    else:
        client.abort.assert_awaited_once_with(f"session-{job['id']}")
    requests = [e for e in t.db.list_audit() if e["action"] == "job.stop.request"]
    assert [e["object_id"] for e in requests] == [job["id"]]
    assert '"reason": "chat.delete"' in requests[0]["metadata_json"]
    deleted = next(e for e in t.db.list_audit() if e["action"] == "chat.delete")
    assert job["id"] in deleted["metadata_json"]
    # The admin queue keeps the cancelled job of the deleted chat.
    await login(t.client)
    assert (await t.client.get(f"/api/jobs/{job['id']}")).status_code == 200


async def test_workspace_change_archives_folder_chats_and_questions_but_not_task_runs(
    ui_app, workspace
):
    t = ui_app
    worker = t.users["worker"]
    question = await make_question(t, "worker")
    run = await make_run(t, "worker")
    folder = await make_folder(t, "worker")
    assert (await t.client.post(f"/api/chats/{folder['chat_id']}/snooze", json={
        "until": future(),
    })).status_code == 200
    for item in (question, run, folder):
        for job in t.db.unfinished_chat_jobs(item["chat_id"]):
            t.db.set_job_status(job["id"], "cancelled")
    moved = workspace / "moved"
    moved.mkdir()

    async def move_to(path):
        await login(t.client)
        response = await t.client.patch(
            f"/api/admin/users/{worker['id']}", json={"workspace_path": str(path)}
        )
        assert response.status_code == 200, response.text

    await move_to(moved)
    for item in (question, folder):
        chat = t.db.get_chat(item["chat_id"])
        assert chat["archived_reason"] == "workspace_changed"
        assert chat["archived_at"] and chat["snoozed_until"] is None
    task_chat = t.db.get_chat(run["chat_id"])
    assert task_chat["archived_reason"] is None and task_chat["archived_at"] is None
    # The old workspace's chats are out of reach until the user moves back.
    await login(t.client, "worker")
    archived_at = t.db.get_chat(folder["chat_id"])["archived_at"]
    response = await t.client.post(f"/api/chats/{folder['chat_id']}/unarchive")
    assert response.status_code == 404
    assert t.db.get_chat(folder["chat_id"])["archived_at"] == archived_at
    assert set(await my_chat_ids(t)) == {run["chat_id"]}

    # Moving back brings the chats of that workspace back.
    await move_to(worker["workspace_path"])
    for item in (question, folder):
        chat = t.db.get_chat(item["chat_id"])
        assert chat["archived_reason"] is None and chat["archived_at"] is None
    await login(t.client, "worker")
    assert set(await my_chat_ids(t)) == {question["chat_id"], run["chat_id"], folder["chat_id"]}


async def test_workspace_change_drops_archived_chats_from_the_owners_live_list(
    ui_app, workspace
):
    t = ui_app
    worker = t.users["worker"]
    question = await make_question(t, "worker")
    run = await make_run(t, "worker")
    folder = await make_folder(t, "worker")
    for item in (question, run, folder):
        for job in t.db.unfinished_chat_jobs(item["chat_id"]):
            t.db.set_job_status(job["id"], "cancelled")
    moved = workspace / "moved"
    moved.mkdir()
    events = t.app.state.events
    published = []
    original = events.publish

    async def capture(event):
        published.append(event)
        await original(event)

    events.publish = capture
    await login(t.client)

    async def move_to(path):
        published.clear()
        response = await t.client.patch(
            f"/api/admin/users/{worker['id']}", json={"workspace_path": str(path)}
        )
        assert response.status_code == 200, response.text
        owner = t.db.get_user(worker["id"])
        admin = t.db.get_user(t.users["admin"]["id"])
        return (
            [event_for_user(t.db, owner, event) for event in published],
            [event_for_user(t.db, admin, event) for event in published],
        )

    for_owner, for_admin = await move_to(moved)
    # The owner's sidebar drops them, as /api/me/items does after a reload.
    assert sorted(for_owner, key=lambda e: e["chat_id"]) == sorted(
        ({"type": "chat.deleted", "chat_id": item["chat_id"]} for item in (question, folder)),
        key=lambda e: e["chat_id"],
    )
    # Admins still see the archive itself.
    assert {e["type"] for e in for_admin} == {"chat.updated"}
    assert all(e["archived_at"] for e in for_admin)

    # Moving back sends the restored rows to the owner again.
    for_owner, _ = await move_to(worker["workspace_path"])
    assert {e["chat_id"] for e in for_owner} == {question["chat_id"], folder["chat_id"]}
    assert all(e["type"] == "chat.updated" and e["archived_at"] is None for e in for_owner)


async def my_chat_ids(t):
    response = await t.client.get("/api/me/items")
    assert response.status_code == 200, response.text
    return [item["chat_id"] for item in response.json()]


async def test_task_run_archived_by_an_old_workspace_change_can_be_unarchived(ui_app):
    t = ui_app
    run = await make_run(t, "worker")
    with t.db.transaction() as connection:
        connection.execute(
            "UPDATE chats SET archived_reason = 'workspace_changed', archived_at = ? "
            "WHERE id = ?",
            (future(-1), run["chat_id"]),
        )
    response = await t.client.post(f"/api/chats/{run['chat_id']}/unarchive")
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None
    chat = t.db.get_chat(run["chat_id"])
    assert chat["archived_reason"] is None and chat["archived_at"] is None


def test_existing_database_gets_chat_state_columns(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path, "s" * 32)
    db.initialize()
    user = db.create_user("legacy", "a-strong-password", "user", str(tmp_path))
    chat = db.create_chat(user["id"], None, "Old", str(tmp_path))
    with db.transaction() as connection:
        connection.execute(
            "UPDATE chats SET archived_reason = 'workspace_changed', updated_at = ? WHERE id = ?",
            ("2025-01-02T03:04:05+00:00", chat["id"]),
        )
        connection.execute("DROP INDEX idx_queue_chat")
        for column in ("archived_at", "snoozed_until", "deleted_at"):
            connection.execute(f"ALTER TABLE chats DROP COLUMN {column}")
    db.close()
    db = Database(path, "s" * 32)
    db.initialize()
    migrated = db.get_chat(chat["id"])
    assert migrated["archived_at"] == "2025-01-02T03:04:05+00:00"
    assert migrated["snoozed_until"] is None and migrated["deleted_at"] is None
    indexes = {row["name"] for row in db._all("PRAGMA index_list(queue_jobs)")}
    assert "idx_queue_chat" in indexes
    db.close()


async def test_workspace_change_keeps_hand_archived_chat_archived(ui_app, workspace):
    t = ui_app
    worker = t.users["worker"]
    kept = await make_folder(t, "worker", "Первый")
    hand = await make_folder(t, "worker", "Второй")
    run = await make_run(t, "worker")
    for item in (kept, hand, run):
        for job in t.db.unfinished_chat_jobs(item["chat_id"]):
            t.db.set_job_status(job["id"], "cancelled")
    await login(t.client, "worker")
    response = await t.client.post(f"/api/chats/{hand['chat_id']}/archive")
    assert response.status_code == 200, response.text
    hand_archived_at = response.json()["archived_at"]
    moved = workspace / "moved"
    moved.mkdir()

    async def move_to(path):
        await login(t.client)
        response = await t.client.patch(
            f"/api/admin/users/{worker['id']}", json={"workspace_path": str(path)}
        )
        assert response.status_code == 200, response.text

    await move_to(moved)
    chat = t.db.get_chat(hand["chat_id"])
    assert chat["archived_reason"] is None and chat["archived_at"] == hand_archived_at
    assert t.db.get_chat(kept["chat_id"])["archived_reason"] == "workspace_changed"

    await move_to(worker["workspace_path"])
    chat = t.db.get_chat(hand["chat_id"])
    assert chat["archived_reason"] is None and chat["archived_at"] == hand_archived_at
    chat = t.db.get_chat(kept["chat_id"])
    assert chat["archived_reason"] is None and chat["archived_at"] is None
    task_chat = t.db.get_chat(run["chat_id"])
    assert task_chat["archived_reason"] is None and task_chat["archived_at"] is None
    await login(t.client, "worker")
    items = {item["chat_id"]: item for item in (await t.client.get("/api/me/items")).json()}
    assert items[hand["chat_id"]]["archived_at"] == hand_archived_at
    assert items[kept["chat_id"]]["archived_at"] is None
