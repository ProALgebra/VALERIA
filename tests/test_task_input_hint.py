from __future__ import annotations

from test_tasks_api import create_task, login
from test_tasks_api import task_app as task_app

from opencode_web.database import Database


async def catalog_hint(t, task_id):
    await login(t.client, "worker")
    tasks = {task["id"]: task for task in (await t.client.get("/api/tasks")).json()}
    return tasks[task_id]["input_hint"]


async def test_admin_sets_and_clears_the_run_input_hint(task_app):
    t = task_app
    task = await create_task(t.client, input_hint="  Ветка или номер PR  ")
    assert task["input_hint"] == "Ветка или номер PR"
    assert await catalog_hint(t, task["id"]) == "Ветка или номер PR"

    await login(t.client)
    path = f"/api/admin/tasks/{task['id']}"
    response = await t.client.patch(path, json={"title": "Новое имя"})
    assert response.status_code == 200 and response.json()["input_hint"] == "Ветка или номер PR"
    response = await t.client.patch(path, json={"input_hint": "Окружение"})
    assert response.status_code == 200 and response.json()["input_hint"] == "Окружение"
    listed = {item["id"]: item for item in (await t.client.get("/api/admin/tasks")).json()}
    assert listed[task["id"]]["input_hint"] == "Окружение"
    assert await catalog_hint(t, task["id"]) == "Окружение"

    await login(t.client)
    for cleared in (None, "   "):
        response = await t.client.patch(path, json={"input_hint": cleared})
        assert response.status_code == 200 and response.json()["input_hint"] is None
    assert await catalog_hint(t, task["id"]) is None
    assert {row["action"] for row in t.db.list_audit()} >= {"task.create", "task.update"}


async def test_input_hint_is_limited_to_500_characters(task_app):
    t = task_app
    task = await create_task(t.client, input_hint="я" * 500)
    assert len(task["input_hint"]) == 500
    await login(t.client)
    response = await t.client.post(
        "/api/admin/tasks",
        json={
            "title": "Длинная", "content": "x", "agent": "build", "model": "provider/coder",
            "input_hint": "я" * 501,
        },
    )
    assert response.status_code == 422
    response = await t.client.patch(
        f"/api/admin/tasks/{task['id']}", json={"input_hint": "я" * 501}
    )
    assert response.status_code == 422
    assert t.db.get_task(task["id"])["input_hint"] == "я" * 500


def test_existing_database_gets_input_hint_column(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path, "s" * 32)
    db.initialize()
    admin = db.create_user("admin", "a-strong-password", "admin", None)
    task = db.create_task(
        admin["id"], "Old", None, "content", "build", None, None, None, True
    )
    with db.transaction() as connection:
        connection.execute("ALTER TABLE tasks DROP COLUMN input_hint")
    db.close()
    db = Database(path, "s" * 32)
    db.initialize()
    db.initialize()
    assert db.get_task(task["id"])["input_hint"] is None
    assert db.update_task(task["id"], {"input_hint": "Подсказка"})["input_hint"] == "Подсказка"
    db.close()
