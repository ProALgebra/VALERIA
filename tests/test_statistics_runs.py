from __future__ import annotations

from datetime import datetime

from test_chat_actions import make_folder, make_question, make_run
from test_chat_actions import ui_app as ui_app
from test_tasks_api import create_task, login

RUN_KEYS = {
    "id", "kind", "task_id", "task_title", "login", "created_at", "started_at", "finished_at",
    "outcome",
}


def job_id(t, chat_id):
    return t.db._one("SELECT id FROM queue_jobs WHERE chat_id = ?", (chat_id,))["id"]


def update_job(t, job, **fields):
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with t.db.transaction() as connection:
        connection.execute(
            f"UPDATE queue_jobs SET {assignments} WHERE id = ?", (*fields.values(), job)
        )


async def runs(t, **params):
    await login(t.client, "admin")
    response = await t.client.get("/api/admin/statistics/runs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def ids(payload):
    return [item["id"] for item in payload["items"]]


async def test_statistics_runs_list_questions_and_task_runs_with_outcomes(ui_app):
    t = ui_app
    question = await make_question(t, "worker")
    await make_folder(t, "worker")
    run = await make_run(t, "worker")
    duplicate = await make_run(t, "worker")
    assert t.db.get_job(duplicate["id"])["error_code"] == "duplicate_task"
    later = await make_question(t, "tasker")
    other_task = await create_task(t.client, title="Релиз")
    await login(t.client, "worker")
    response = await t.client.post(f"/api/tasks/{other_task['id']}/runs", json={})
    other_run = response.json()["id"]
    await login(t.client, "worker")
    assert (await t.client.delete(f"/api/chats/{question['chat_id']}")).status_code == 204

    question_job, later_job = job_id(t, question["chat_id"]), job_id(t, later["chat_id"])
    update_job(
        t, run["id"], status="completed", started_at="2026-03-01T10:01:00+00:00",
        finished_at="2026-03-01T10:05:00+00:00", created_at="2026-03-01T10:00:00+00:00",
    )
    update_job(t, later_job, status="running", created_at="2026-03-02T23:30:00+00:00")
    update_job(t, other_run, status="failed", created_at="2026-03-03T08:00:00+00:00")
    update_job(t, question_job, created_at="2026-02-28T12:00:00+00:00")

    payload = await runs(t)
    assert set(payload) == {"items", "generated_at"}
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
    # Deleted chats still count; folder chats and deduplicated launches do not.
    assert ids(payload) == [question_job, run["id"], later_job, other_run]
    items = {item["id"]: item for item in payload["items"]}
    assert all(set(item) == RUN_KEYS for item in payload["items"])
    assert items[run["id"]] == {
        "id": run["id"], "kind": "task", "task_id": t.task["id"],
        "task_title": t.task["title"], "login": "worker", "created_at": "2026-03-01T10:00:00+00:00",
        "started_at": "2026-03-01T10:01:00+00:00", "finished_at": "2026-03-01T10:05:00+00:00",
        "outcome": "completed",
    }
    assert items[question_job]["kind"] == "question" and items[question_job]["task_id"] is None
    # Deleting the chat cancelled its queued question; a running one has no outcome yet.
    assert items[question_job]["outcome"] == "cancelled"
    assert items[later_job]["outcome"] is None
    assert items[other_run]["outcome"] == "failed"
    assert items[other_run]["task_title"] == "Релиз"
    # The owner's login comes from the chat's user, deleted chats included.
    assert items[question_job]["login"] == "worker"
    assert items[later_job]["login"] == "tasker"

    assert ids(await runs(t, type="question")) == [question_job, later_job]
    assert ids(await runs(t, type="task")) == [run["id"], other_run]
    assert ids(await runs(t, type=other_task["id"])) == [other_run]
    assert ids(await runs(t, type="missing-task")) == []

    assert ids(await runs(t, **{"from": "2026-03-01", "to": "2026-03-02"})) == [
        run["id"], later_job,
    ]
    assert ids(await runs(
        t, **{"from": "2026-03-03", "to": "2026-03-03", "timezone": "Europe/Moscow"}
    )) == [later_job, other_run]
    assert ids(await runs(t, type="task", **{"to": "2026-03-01T10:00:00Z"})) == []


async def test_statistics_runs_validation_and_access(ui_app):
    t = ui_app
    await login(t.client, "admin")
    for params in ({"from": "soon"}, {"timezone": "Mars/Base"}):
        response = await t.client.get("/api/admin/statistics/runs", params=params)
        assert response.status_code == 422, params
    for name in ("worker", "tasker"):
        await login(t.client, name)
        assert (await t.client.get("/api/admin/statistics/runs")).status_code == 403
