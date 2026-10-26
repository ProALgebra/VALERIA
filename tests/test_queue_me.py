from __future__ import annotations

from test_chat_actions import make_folder, make_question, make_run
from test_chat_actions import ui_app as ui_app
from test_tasks_api import login


async def run_with_input(t, name, user_input):
    # Identical pending runs of one task are deduplicated, so vary the input.
    await login(t.client, name)
    response = await t.client.post(
        f"/api/tasks/{t.task['id']}/runs", json={"user_input": user_input}
    )
    assert response.status_code == 202, response.text
    run = response.json()
    return {"chat_id": t.db.get_job(run["id"])["chat_id"], "id": run["id"]}


async def queue_of(t, name):
    await login(t.client, name)
    response = await t.client.get("/api/queue/me")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


async def test_queue_me_lists_every_kind_with_its_position(ui_app):
    t = ui_app
    question = await make_question(t, "worker")
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    other = await make_question(t, "tasker")
    tasker_run = await run_with_input(t, "tasker", "ветка main")
    job_of = {
        made["chat_id"]: t.db.unfinished_chat_jobs(made["chat_id"])[0]["id"]
        for made in (question, folder, run, other, tasker_run)
    }

    worker = await queue_of(t, "worker")
    assert {row["kind"] for row in worker.values()} == {"question", "folder", "task"}
    positions = {row["chat_id"]: row["position"] for row in worker.values()}
    # Positions count everyone's queued jobs ahead, in claim order.
    assert positions == {question["chat_id"]: 1, folder["chat_id"]: 2, run["chat_id"]: 3}
    folder_row = worker[job_of[folder["chat_id"]]]
    assert folder_row["content"] == "Запусти тесты" and folder_row["status"] == "queued"
    for row in worker.values():
        if row["kind"] != "folder":
            assert "content" not in row and "session_id" not in row

    tasker = await queue_of(t, "tasker")
    assert {row["chat_id"]: row["position"] for row in tasker.values()} == {
        other["chat_id"]: 4, tasker_run["chat_id"]: 5,
    }

    # A restart continuation goes first, then higher-priority queues, then FIFO.
    urgent = t.db.create_work_queue("Срочная", 10, 1)
    with t.db.transaction() as connection:
        connection.execute(
            "UPDATE queue_jobs SET queue_id = ? WHERE id = ?",
            (urgent["id"], job_of[run["chat_id"]]),
        )
        connection.execute(
            "UPDATE queue_jobs SET resume_content = 'continue' WHERE id = ?",
            (job_of[tasker_run["chat_id"]],),
        )
    assert t.db.queue_positions() == {
        job_of[tasker_run["chat_id"]]: 1, job_of[run["chat_id"]]: 2,
        job_of[question["chat_id"]]: 3, job_of[folder["chat_id"]]: 4,
        job_of[other["chat_id"]]: 5,
    }

    # Started, waiting and finished jobs have no place in the line.
    claimed = t.db.claim_next_job(5)
    t.db.set_job_status(claimed["id"], "waiting_question")
    tasker = await queue_of(t, "tasker")
    assert tasker[claimed["id"]]["status"] == "waiting_question"
    assert tasker[claimed["id"]]["position"] is None
    assert tasker[job_of[other["chat_id"]]]["position"] == 4


async def test_queue_me_hides_folder_jobs_from_tasks_only_and_deleted_chats(ui_app):
    t = ui_app
    question = await make_question(t, "tasker")
    legacy = t.db.create_chat(
        t.users["tasker"]["id"], None, "Legacy", t.users["tasker"]["workspace_path"]
    )
    t.db.create_job(
        chat=legacy, effective_user_id=t.users["tasker"]["id"],
        actor_user_id=t.users["tasker"]["id"], content="old", agent="build",
        provider_id="test", model_id="coder", model_variant=None,
    )
    assert {row["chat_id"] for row in (await queue_of(t, "tasker")).values()} == {
        question["chat_id"]
    }
    assert (await t.client.delete(f"/api/chats/{question['chat_id']}")).status_code == 204
    assert await queue_of(t, "tasker") == {}


async def test_queue_me_drops_folder_jobs_and_questions_of_an_old_workspace(ui_app, workspace):
    t = ui_app
    worker = t.users["worker"]
    question = await make_question(t, "worker")
    folder = await make_folder(t, "worker")
    run = await make_run(t, "worker")
    for made in (question, folder, run):
        for job in t.db.unfinished_chat_jobs(made["chat_id"]):
            t.db.set_job_status(job["id"], "completed")
    moved = workspace / "moved"
    moved.mkdir()
    await login(t.client)
    response = await t.client.patch(
        f"/api/admin/users/{worker['id']}", json={"workspace_path": str(moved)}
    )
    assert response.status_code == 200, response.text

    rows = await queue_of(t, "worker")
    assert {row["chat_id"] for row in rows.values()} == {run["chat_id"]}

    fresh = await make_folder(t, "worker", "Новая папка")
    rows = await queue_of(t, "worker")
    assert {row["chat_id"] for row in rows.values()} == {run["chat_id"], fresh["chat_id"]}
