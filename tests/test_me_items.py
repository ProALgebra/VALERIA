from __future__ import annotations

from test_chat_actions import make_folder, make_question, make_run
from test_chat_actions import ui_app as ui_app
from test_tasks_api import login

ITEM_KEYS = {
    "kind", "id", "chat_id", "title", "status", "phase", "task_id", "created_at",
    "updated_at", "archived_at", "snoozed_until",
}
REQUEST_KEYS = {
    "kind", "id", "chat_id", "title", "status", "user", "task_id", "task_title",
    "created_at", "updated_at", "deleted_at",
}


def set_status(t, chat_id, status):
    with t.db.transaction() as connection:
        connection.execute("UPDATE queue_jobs SET status = ? WHERE chat_id = ?", (status, chat_id))


def set_created(t, chat_id, created_at):
    with t.db.transaction() as connection:
        connection.execute("UPDATE chats SET created_at = ? WHERE id = ?", (created_at, chat_id))


async def my_items(t, name):
    await login(t.client, name)
    response = await t.client.get("/api/me/items")
    assert response.status_code == 200, response.text
    return {item["chat_id"]: item for item in response.json()}


async def test_my_items_project_every_kind_with_normalized_status(ui_app):
    t = ui_app
    question = await make_question(t, "worker")
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    await login(t.client, "worker")
    empty = (await t.client.post("/api/chats", json={})).json()["chat"]["id"]
    await make_question(t, "stranger")

    items = await my_items(t, "worker")
    assert set(items) == {question["chat_id"], folder["chat_id"], run["chat_id"], empty}
    assert all(set(item) == ITEM_KEYS for item in items.values())
    assert items[question["chat_id"]] | {"created_at": None, "updated_at": None} == {
        "kind": "question", "id": question["chat_id"], "chat_id": question["chat_id"],
        "title": "Как настроить CI?", "status": "queued", "phase": None, "task_id": None,
        "created_at": None, "updated_at": None, "archived_at": None, "snoozed_until": None,
    }
    task_item = items[run["chat_id"]]
    assert (task_item["kind"], task_item["id"], task_item["task_id"]) == (
        "task", run["id"], t.task["id"],
    )
    assert task_item["title"] == t.task["title"] and task_item["phase"] == "build"
    assert items[folder["chat_id"]]["status"] == "queued"
    assert items[empty]["status"] == "idle" and items[empty]["kind"] == "folder"

    for status in ("dispatching", "running", "waiting_permission", "cancelling"):
        set_status(t, folder["chat_id"], status)
        assert (await my_items(t, "worker"))[folder["chat_id"]]["status"] == status
    for status in ("completed", "failed", "cancelled"):
        set_status(t, folder["chat_id"], status)
        set_status(t, question["chat_id"], status)
        set_status(t, run["chat_id"], status)
        items = await my_items(t, "worker")
        assert items[folder["chat_id"]]["status"] == "idle"
        assert items[question["chat_id"]]["status"] == status
        assert items[run["chat_id"]]["status"] == status
    for status in ("waiting_question", "waiting_permission"):
        set_status(t, run["chat_id"], status)
        set_status(t, question["chat_id"], status)
        items = await my_items(t, "worker")
        assert items[run["chat_id"]]["status"] == status
        assert items[question["chat_id"]]["status"] == status

    response = await t.client.post(f"/api/chats/{question['chat_id']}/archive")
    assert response.status_code == 200, response.text
    assert (await my_items(t, "worker"))[question["chat_id"]]["archived_at"]


async def test_my_items_scope_to_owner_workspace_and_role(ui_app):
    t = ui_app
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    gone = await make_question(t, "worker")
    with t.db.transaction() as connection:
        connection.execute(
            "UPDATE chats SET workspace_snapshot = '/elsewhere' WHERE id IN (?, ?)",
            (folder["chat_id"], run["chat_id"]),
        )
    await login(t.client, "worker")
    assert (await t.client.delete(f"/api/chats/{gone['chat_id']}")).status_code == 204
    # Task runs follow the user; folder chats belong to the workspace they ran in.
    assert set(await my_items(t, "worker")) == {run["chat_id"]}

    question = await make_question(t, "tasker")
    tasker_run = await make_run(t, "tasker")
    legacy = t.db.create_chat(
        t.users["tasker"]["id"], None, "Legacy", t.users["tasker"]["workspace_path"]
    )
    items = await my_items(t, "tasker")
    assert legacy["id"] not in items
    assert {item["kind"] for item in items.values()} == {"question", "task"}
    assert set(items) == {question["chat_id"], tasker_run["chat_id"]}
    assert await my_items(t, "admin") == {}


async def requests(t, **params):
    await login(t.client, "admin")
    response = await t.client.get("/api/admin/requests", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def chat_ids(rows):
    return {row["chat_id"] for row in rows}


async def test_admin_requests_filter_by_user_type_task_dates_and_text(ui_app):
    t = ui_app
    question = await make_question(t, "worker", "Почему Château падает?")
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    other = await make_question(t, "tasker", "Как выпустить релиз")
    deleted = await make_question(t, "stranger", "Удалённый вопрос")
    await login(t.client, "stranger")
    assert (await t.client.delete(f"/api/chats/{deleted['chat_id']}")).status_code == 204

    rows = await requests(t)
    everything = {question["chat_id"], folder["chat_id"], run["chat_id"], other["chat_id"],
                  deleted["chat_id"]}
    assert chat_ids(rows) == everything
    assert all(set(row) == REQUEST_KEYS for row in rows)
    by_chat = {row["chat_id"]: row for row in rows}
    assert by_chat[run["chat_id"]] | {"created_at": None, "updated_at": None} == {
        "kind": "task", "id": run["id"], "chat_id": run["chat_id"], "title": t.task["title"],
        "status": "queued",
        "user": {"id": t.users["worker"]["id"], "login": "worker"},
        "task_id": t.task["id"], "task_title": t.task["title"],
        "created_at": None, "updated_at": None, "deleted_at": None,
    }
    assert by_chat[question["chat_id"]]["task_title"] is None
    assert by_chat[deleted["chat_id"]]["deleted_at"]

    assert chat_ids(await requests(t, user=" Worker , TASKER")) == everything - {
        deleted["chat_id"]
    }
    assert chat_ids(await requests(t, type="question,folder", user="worker")) == {
        question["chat_id"], folder["chat_id"],
    }
    assert chat_ids(await requests(t, type="task")) == {run["chat_id"]}
    assert chat_ids(await requests(t, task=t.task["id"])) == {run["chat_id"]}
    assert await requests(t, task="missing") == []

    # Case, accents, word order and quoted phrases.
    assert chat_ids(await requests(t, q="  chateau ПАДАЕТ ")) == {question["chat_id"]}
    assert chat_ids(await requests(t, q='"château падает"')) == {question["chat_id"]}
    assert await requests(t, q='"падает chateau"') == []
    assert chat_ids(await requests(t, q="tasker релиз")) == {other["chat_id"]}
    assert chat_ids(await requests(t, q="проверка worker")) == {run["chat_id"]}
    assert chat_ids(await requests(t, q="")) == everything

    set_created(t, question["chat_id"], "2026-01-10T12:00:00+00:00")
    set_created(t, other["chat_id"], "2026-01-11T09:00:00+00:00")
    assert chat_ids(await requests(t, **{"from": "2026-01-10", "to": "2026-01-10"})) == {
        question["chat_id"]
    }
    assert chat_ids(await requests(t, **{"from": "2026-01-10", "to": "2026-01-11"})) == {
        question["chat_id"], other["chat_id"],
    }
    # Explicit times are exact; `to` is exclusive.
    assert chat_ids(await requests(
        t, **{"from": "2026-01-10T14:00:00+03:00", "to": "2026-01-11T09:00:00Z"}
    )) == {question["chat_id"]}
    # Bare dates are whole days in `timezone` (UTC by default).
    assert chat_ids(await requests(t, **{"from": "2026-01-11", "to": "2026-01-11"})) == {
        other["chat_id"]
    }
    assert chat_ids(await requests(
        t, **{"from": "2026-01-11", "to": "2026-01-11", "timezone": "Pacific/Kiritimati"}
    )) == {question["chat_id"], other["chat_id"]}
    # An unencoded "+" in the offset still parses.
    response = await t.client.get(
        "/api/admin/requests?from=2026-01-10T16:00:00+03:00&to=2026-01-12"
    )
    assert chat_ids(response.json()) == {other["chat_id"]}


async def test_admin_requests_validation_access_and_detail_audit(ui_app):
    t = ui_app
    question = await make_question(t, "worker")
    await login(t.client, "admin")
    for params in ({"type": "chat"}, {"from": "yesterday"}, {"timezone": "Mars/Base"}):
        response = await t.client.get("/api/admin/requests", params=params)
        assert response.status_code == 422, params
    for name in ("worker", "tasker"):
        await login(t.client, name)
        assert (await t.client.get("/api/admin/requests")).status_code == 403

    row = (await requests(t, type="question"))[0]
    response = await t.client.get(f"/api/admin/consultations/{row['id']}")
    assert response.status_code == 200
    viewed = [entry for entry in t.db.list_audit() if entry["action"] == "consultation.admin_view"]
    assert viewed[0]["object_id"] == question["chat_id"]
    assert viewed[0]["effective_user_id"] == t.users["worker"]["id"]
