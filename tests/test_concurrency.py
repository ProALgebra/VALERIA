from __future__ import annotations

import sqlite3

import pytest
from test_chat_actions import ui_app as ui_app
from test_tasks_api import create_task, login

from opencode_web.database import Database

TABLES = ("users", "tasks", "work_queues")


async def patch(t, path, body):
    return await t.client.patch(path, json=body)


def as_other_admin(t, object_table, object_id):
    """Pretend a second admin edited the row a moment later."""
    other = t.db.create_user("second-admin", "a-strong-password", "admin", None)
    with t.db.transaction() as connection:
        connection.execute(
            f"UPDATE {object_table} SET updated_at = '2099-01-01T00:00:00+00:00', "
            "updated_by = ? WHERE id = ?",
            (other["id"], object_id),
        )
    return other


async def test_task_patch_detects_conflicts_and_deletion(ui_app):
    t = ui_app
    task = await create_task(t.client, title="Сборка")
    path = f"/api/admin/tasks/{task['id']}"
    stamp = task["updated_at"]
    assert task["updated_by"] == t.users["admin"]["id"]

    # The same instant written differently still matches.
    same = stamp.replace("+00:00", "Z")
    response = await patch(t, path, {"title": "Сборка 2", "updated_at": same})
    assert response.status_code == 200, response.text
    fresh = response.json()["updated_at"]
    assert fresh != stamp

    # A stale stamp is rejected and nothing changes.
    response = await patch(t, path, {"title": "Перезапись", "updated_at": stamp})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "conflict", "updated_by": "admin", "updated_at": fresh,
    }
    assert t.db.get_task(task["id"])["title"] == "Сборка 2"

    as_other_admin(t, "tasks", task["id"])
    response = await patch(t, path, {"title": "Моя версия", "updated_at": fresh})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "conflict", "updated_by": "second-admin",
        "updated_at": "2099-01-01T00:00:00+00:00",
    }
    # Without a stamp the last write still wins.
    response = await patch(t, path, {"title": "Моя версия"})
    assert response.status_code == 200 and response.json()["title"] == "Моя версия"

    assert (await t.client.delete(path)).status_code == 204
    for body in ({"title": "Поздно"}, {"title": "Поздно", "updated_at": fresh}):
        response = await patch(t, path, body)
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "deleted", "deleted_by": "admin"}
    response = await patch(t, "/api/admin/tasks/missing", {"title": "Нет"})
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "deleted", "deleted_by": None}

    response = await patch(t, f"/api/admin/tasks/{t.task['id']}", {"updated_at": "2026-01-01"})
    assert response.status_code == 422  # a stamp needs a time zone


async def test_stale_task_patch_keeps_successors(ui_app):
    t = ui_app
    task = await create_task(t.client, title="Корень")
    child = await create_task(t.client, title="Следующая")
    as_other_admin(t, "tasks", task["id"])
    response = await patch(t, f"/api/admin/tasks/{task['id']}", {
        "next_task_ids": [child["id"]], "updated_at": task["updated_at"],
    })
    assert response.status_code == 409
    assert t.db.list_task_successors(task["id"]) == []
    # The compare-and-set itself rolls the successors back when the row moved on.
    assert t.db.update_task(
        task["id"], {"next_task_ids": [child["id"]]}, expected_updated_at=task["updated_at"]
    ) is None
    assert t.db.list_task_successors(task["id"]) == []


async def test_user_patch_detects_conflicts_and_missing_users(ui_app):
    t = ui_app
    worker = t.users["worker"]
    path = f"/api/admin/users/{worker['id']}"
    stamp = t.db.get_user(worker["id"])["updated_at"]

    # Signing in is not an edit and does not invalidate open forms.
    await login(t.client, "worker")
    await login(t.client, "admin")
    assert t.db.get_user(worker["id"])["updated_at"] == stamp

    response = await patch(t, path, {"max_active_jobs": 1, "updated_at": stamp})
    assert response.status_code == 200, response.text
    fresh = response.json()["updated_at"]
    assert t.db.edit_state("users", worker["id"])["updated_by"] == "admin"

    response = await patch(t, path, {"status": "disabled", "updated_at": stamp})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "conflict", "updated_by": "admin", "updated_at": fresh,
    }
    assert t.db.get_user(worker["id"])["status"] == "active"

    # Batch blocks count as edits by the admin who ran them.
    response = await t.client.post(
        "/api/admin/users/batch", json={"ids": [worker["id"]], "status": "blocked"}
    )
    assert response.status_code == 200
    response = await patch(t, path, {"status": "active", "updated_at": fresh})
    assert response.status_code == 409
    assert response.json()["detail"]["updated_by"] == "admin"

    response = await patch(t, "/api/admin/users/missing", {"status": "active"})
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "deleted", "deleted_by": None}


async def test_password_reset_records_who_made_it(ui_app):
    t = ui_app
    stranger = t.users["stranger"]
    await login(t.client, "admin")
    response = await t.client.post(
        f"/api/admin/users/{stranger['id']}/reset-password",
        json={"password": "another-strong-password"},
    )
    assert response.status_code == 204, response.text
    assert t.db.edit_state("users", stranger["id"])["updated_by"] == "admin"


async def test_queue_patch_detects_conflicts_and_archiving(ui_app):
    t = ui_app
    await login(t.client, "admin")
    response = await t.client.post(
        "/api/admin/queues", json={"name": "Ночная", "priority": 1, "max_workers": 1}
    )
    assert response.status_code == 201, response.text
    queue = response.json()
    path = f"/api/admin/queues/{queue['id']}"
    body = {"name": "Ночная", "priority": 2, "max_workers": 1}

    response = await patch(t, path, body | {"updated_at": queue["updated_at"]})
    assert response.status_code == 200, response.text
    fresh = response.json()["updated_at"]
    response = await patch(t, path, body | {"priority": 3, "updated_at": queue["updated_at"]})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "conflict", "updated_by": "admin", "updated_at": fresh,
    }
    assert t.db.get_work_queue(queue["id"])["priority"] == 2

    assert (await t.client.delete(path)).status_code == 204
    other = t.db.create_user("second-admin", "a-strong-password", "admin", None)
    assert t.db.archive_work_queue(queue["id"], other["id"])  # archiving again changes nothing
    for extra in ({}, {"updated_at": fresh}):
        response = await patch(t, path, body | extra)
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "deleted", "deleted_by": "admin"}
    assert t.db.get_work_queue(queue["id"])["priority"] == 2
    response = await patch(t, "/api/admin/queues/missing", body)
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "deleted", "deleted_by": None}


def test_updated_by_columns_are_added_to_old_databases(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path, "s" * 32)
    db.initialize()
    with db.transaction() as connection:
        for table in TABLES:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN updated_by")
    db.close()

    db = Database(path, "s" * 32)
    db.initialize()
    db.initialize()
    for table in TABLES:
        columns = {row["name"] for row in db._all(f"PRAGMA table_info({table})")}
        assert "updated_by" in columns, table
    db.close()


async def test_task_patch_writes_fields_and_audience_as_one_change(ui_app):
    t = ui_app
    task = await create_task(t.client, title="Сборка")
    path = f"/api/admin/tasks/{task['id']}"
    worker = t.users["worker"]["id"]
    audience = {"visibility": "selected", "user_ids": [worker]}

    response = await patch(
        t, path, {"title": "Новое", **audience, "updated_at": "2000-01-01T00:00:00+00:00"}
    )
    assert response.status_code == 409
    stored = t.db.get_task(task["id"])
    assert (stored["title"], stored["visibility"]) == ("Сборка", task["visibility"])
    assert t.db.list_task_audience(task["id"]) == []

    response = await patch(
        t, path, {"title": "Новое", **audience, "updated_at": task["updated_at"]}
    )
    assert response.status_code == 200, response.text
    stored = t.db.get_task(task["id"])
    assert (stored["title"], stored["visibility"]) == ("Новое", "selected")
    assert t.db.list_task_audience(task["id"]) == [worker]
    assert response.json()["updated_at"] == stored["updated_at"]

    # Changing only one of the two keeps the other.
    response = await patch(
        t, path, {"visibility": "all", "updated_at": stored["updated_at"]}
    )
    assert response.status_code == 200, response.text
    assert t.db.get_task(task["id"])["visibility"] == "all"
    response = await patch(t, path, {"visibility": "selected"})
    assert response.status_code == 200, response.text
    assert t.db.list_task_audience(task["id"]) == []


async def test_failed_audience_write_rolls_back_the_task_fields(ui_app):
    t = ui_app
    task = await create_task(t.client, title="Сборка")
    with pytest.raises(sqlite3.IntegrityError):
        t.db.update_task(
            task["id"], {"title": "Новое"}, visibility="selected", user_ids=["no-such-user"],
            expected_updated_at=task["updated_at"],
        )
    stored = t.db.get_task(task["id"])
    assert (stored["title"], stored["visibility"]) == ("Сборка", task["visibility"])
    assert stored["updated_at"] == task["updated_at"]
