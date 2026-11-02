from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_task_database import start_task
from test_task_database import task_database as task_database
from test_tasks_api import create_task, login
from test_tasks_api import task_app as task_app

from opencode_web.app import event_for_user
from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker


def pipeline(database, admin, root):
    children = [
        database.create_task(
            admin["id"],
            title,
            "",
            f"Instructions for {title}",
            "build",
            "provider",
            "model",
            None,
            False,
        )
        for title in ("B", "C", "D")
    ]
    b, c, d = children
    database.update_task(root["id"], {"next_task_ids": [b["id"], c["id"]]})
    database.update_task(b["id"], {"next_task_ids": [d["id"]]})
    for child in children:
        database.set_task_audience(child["id"], "selected", [])
    return database.get_task(root["id"]), b, c, d


def test_branching_copies_input_and_identity_bypassing_availability(task_database):
    db, admin, user, root = task_database
    root, b, c, d = pipeline(db, admin, root)
    child_queue = db.create_work_queue("Pipeline B", 20, 1)
    db.update_task(b["id"], {"queue_id": child_queue["id"]})
    run = start_task(db, user, root, "root", user_input="Данные\nстрока 2")
    assert db.pipeline_children(run["id"]) == []
    db.set_job_status(run["id"], "completed")
    children = db.pipeline_children(run["id"])
    assert {child["task_id"] for child in children} == {b["id"], c["id"]}
    for child in children:
        assert child["effective_user_id"] == child["actor_user_id"] == user["id"]
        assert child["user_input"] == run["user_input"]
        assert child["workspace_snapshot"] == run["workspace_snapshot"]
        assert child["status"] == "queued"
        assert child["session_pending"] == 1
        assert child["content"].endswith("\n\n" + run["user_input"])
        assert not db.task_visible_to(child["task_id"], user["id"])
        assert db.get_chat(child["chat_id"])["owner_user_id"] == user["id"]
    assert next(child for child in children if child["task_id"] == b["id"])["queue_id"] == (
        child_queue["id"]
    )
    assert next(child for child in children if child["task_id"] == c["id"])["queue_id"] == (
        "default"
    )
    # Branches can run concurrently, subject to the existing concurrency limit.
    assert db.claim_next_job(2)["id"] in {child["id"] for child in children}
    assert db.claim_next_job(2)["id"] in {child["id"] for child in children}
    assert db.claim_next_job(2) is None
    b_run = next(child for child in children if child["task_id"] == b["id"])
    db.set_job_status(b_run["id"], "completed")
    (grandchild,) = db.pipeline_children(b_run["id"])
    assert grandchild["task_id"] == d["id"]
    assert grandchild["user_input"] == run["user_input"]
    assert grandchild["actor_user_id"] == user["id"]
    assert len([a for a in db.list_audit() if a["action"] == "task.pipeline.run"]) == 3


@pytest.mark.parametrize("status", ["failed", "cancelled", "cancelling"])
def test_unsuccessful_parent_never_starts_children(task_database, status):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    db.set_job_status(run["id"], status)
    db.set_job_status(run["id"], "completed")  # A late completion must not resume this branch.
    assert db.pipeline_children(run["id"]) == []
    assert db.get_job(run["id"])["status"] == status


def test_completion_is_atomic_idempotent_and_survives_restart(task_database):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    reopened = Database(db.path, "x" * 32)
    reopened.initialize()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(connection.set_job_status, run["id"], "completed")
                for connection in (db, reopened)
            ]
            for future in futures:
                future.result(timeout=10)
        assert len(db.pipeline_children(run["id"])) == 2
        reopened.initialize()
        reopened.set_job_status(run["id"], "completed")
        assert len(reopened.pipeline_children(run["id"])) == 2
        assert reopened.queued_count() == 2
    finally:
        reopened.close()


def test_completion_rolls_back_if_child_cannot_be_persisted(task_database, monkeypatch):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    original = db._enqueue_pipeline_children

    def fail_after_insert(connection, parent):
        original(connection, parent)
        raise RuntimeError("disk error")

    monkeypatch.setattr(db, "_enqueue_pipeline_children", fail_after_insert)
    with pytest.raises(RuntimeError, match="disk error"):
        db.set_job_status(run["id"], "completed")
    assert db.get_job(run["id"])["status"] == "queued"
    assert db.pipeline_children(run["id"]) == []
    assert len(db.list_task_runs()) == 1
    monkeypatch.setattr(db, "_enqueue_pipeline_children", original)
    db.set_job_status(run["id"], "completed")
    assert len(db.pipeline_children(run["id"])) == 2


def test_snapshot_prevents_cycles_from_edits_during_run(task_database):
    db, admin, user, root = task_database
    root, b, c, d = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    db.update_task(root["id"], {"next_task_ids": []})
    db.update_task(b["id"], {"next_task_ids": [root["id"]], "content": "New prompt"})
    db.delete_task(c["id"])
    db.set_job_status(run["id"], "completed")
    children = db.pipeline_children(run["id"])
    assert len(children) == 2
    b_run = next(child for child in children if child["task_id"] == b["id"])
    assert b_run["content"].startswith(b["content"])
    db.set_job_status(b_run["id"], "completed")
    (grandchild,) = db.pipeline_children(b_run["id"])
    assert grandchild["task_id"] == d["id"]
    db.set_job_status(grandchild["id"], "completed")
    assert db.pipeline_children(grandchild["id"]) == []


def test_separate_pipelines_do_not_cancel_identical_children(task_database):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    first = start_task(db, user, root, "first")
    db.set_job_status(first["id"], "completed")
    second = start_task(db, user, root, "second")
    db.set_job_status(second["id"], "completed")
    assert len(db.pipeline_children(first["id"])) == len(db.pipeline_children(second["id"])) == 2
    assert db.queued_count() == 4


async def test_admin_api_validates_links_atomically_and_hides_them(task_app):
    t = task_app
    child = await create_task(
        t.client, title="Hidden child", is_published=False, visibility="selected", user_ids=[]
    )
    root = await create_task(t.client, next_task_ids=[child["id"], child["id"]])
    assert root["next_task_ids"] == [child["id"]]
    # Omission preserves links; an explicit empty list clears them.
    response = await t.client.patch(f"/api/admin/tasks/{root['id']}", json={"title": "Root"})
    assert response.json()["next_task_ids"] == [child["id"]]
    for target, next_id in ((root, root["id"]), (child, root["id"]), (root, "missing")):
        response = await t.client.patch(
            f"/api/admin/tasks/{target['id']}",
            json={
                "title": "Must roll back",
                "next_task_ids": [next_id],
            },
        )
        assert response.status_code == 422, response.text
        assert t.db.get_task(target["id"])["title"] != "Must roll back"
    response = await t.client.patch(f"/api/admin/tasks/{root['id']}", json={"next_task_ids": None})
    assert response.status_code == 422
    count = len(t.db.list_tasks(published_only=False))
    response = await t.client.post(
        "/api/admin/tasks",
        json={
            "title": "Invalid",
            "content": "Prompt",
            "agent": "build",
            "model": "provider/model",
            "next_task_ids": ["missing"],
        },
    )
    assert response.status_code == 422
    assert len(t.db.list_tasks(published_only=False)) == count
    user = await login(t.client, "tasker")
    assert (await t.client.get("/api/admin/tasks")).status_code == 403
    assert (
        await t.client.patch(f"/api/admin/tasks/{root['id']}", json={"next_task_ids": []})
    ).status_code == 403
    tasks = (await t.client.get("/api/tasks")).json()
    assert [task["id"] for task in tasks] == [root["id"]]
    assert "next_task_ids" not in tasks[0]
    assert (await t.client.post(f"/api/tasks/{child['id']}/runs", json={})).status_code == 404
    response = await t.client.post(
        f"/api/tasks/{root['id']}/runs", json={"user_input": "Общий ввод\n123"}
    )
    assert response.status_code == 202, response.text
    run = response.json()
    t.db.set_job_status(run["id"], "completed")
    (child_run,) = t.db.pipeline_children(run["id"])
    assert child_run["user_input"] == "Общий ввод\n123"
    assert child_run["effective_user_id"] == user["id"]
    public_runs = (await t.client.get("/api/task-runs")).json()
    assert len(public_runs) == 2
    for public in public_runs:
        assert not {"pipeline_json", "parent_job_id", "content", "next_task_ids"} & public.keys()
    event = event_for_user(t.db, user, {"type": "job.updated", "job": child_run})
    assert "pipeline_json" not in event["task"]
    assert "parent_job_id" not in event["task"]
    await login(t.client)
    response = await t.client.patch(f"/api/admin/tasks/{root['id']}", json={"next_task_ids": []})
    assert response.json()["next_task_ids"] == []


async def test_dispatcher_creates_child_session_and_recovers_before_prompt(task_database):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    db.set_job_status(run["id"], "completed")
    child = db.claim_next_job(1)
    assert child["session_pending"] == 1
    client = SimpleNamespace(
        create_session=AsyncMock(return_value={"id": "real-session"}),
        send_prompt_async=AsyncMock(),
    )
    manager = SimpleNamespace(ensure=AsyncMock(return_value=client))
    events = SimpleNamespace(publish=AsyncMock())
    dispatcher = QueueDispatcher(db, manager, events, 1)
    # A crash after claiming and before creating a session is safe to retry.
    await dispatcher.recover()
    manager.ensure.assert_not_awaited()
    assert db.get_job(child["id"])["status"] == "queued"
    dispatcher._monitor = AsyncMock()
    await dispatcher._execute(db.claim_next_job(1))
    stored = db.get_job(child["id"])
    assert stored["session_pending"] == 0
    assert stored["session_id"] == "real-session"
    assert db.get_chat(child["chat_id"])["opencode_session_id"] == "real-session"
    client.send_prompt_async.assert_awaited_once()
    assert client.send_prompt_async.call_args.args == ("real-session",)
    assert client.send_prompt_async.call_args.kwargs["content"] == child["content"]
    assert dispatcher._monitor.call_args.args[0]["session_id"] == "real-session"


async def test_recovered_completed_parent_enqueues_children_once(task_database):
    db, admin, user, root = task_database
    root, *_ = pipeline(db, admin, root)
    run = start_task(db, user, root, "root")
    db.claim_next_job(1)
    client = SimpleNamespace(
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        messages=AsyncMock(
            return_value=[
                {"info": {"id": run["message_id"], "role": "user"}},
                {
                    "info": {
                        "role": "assistant",
                        "parentID": run["message_id"],
                        "finish": "stop",
                        "time": {"completed": 123},
                    }
                },
            ]
        ),
    )
    dispatcher = QueueDispatcher(
        db, SimpleNamespace(ensure=AsyncMock(return_value=client)), EventBroker(), 1
    )
    await dispatcher.recover()
    assert db.get_job(run["id"])["status"] == "completed"
    assert len(db.pipeline_children(run["id"])) == 2
    await dispatcher.recover()
    assert len(db.pipeline_children(run["id"])) == 2


async def test_dispatcher_executes_entire_pipeline_with_one_input(task_database, monkeypatch):
    import asyncio

    db, admin, user, root = task_database
    root, b, c, d = pipeline(db, admin, root)
    run = start_task(db, user, root, "root", user_input="shared\ninput")
    prompts = {}
    sleep = asyncio.sleep

    async def fast_sleep(_):
        await sleep(0)

    async def create_session(title, **kwargs):
        return {"id": f"session-{len(prompts)}"}

    async def send_prompt(session_id, **kwargs):
        assert session_id not in prompts
        prompts[session_id] = kwargs

    async def messages(session_id):
        message_id = prompts[session_id]["message_id"]
        return [
            {"info": {"id": message_id, "role": "user"}},
            {
                "info": {
                    "role": "assistant",
                    "parentID": message_id,
                    "finish": "stop",
                    "time": {"completed": 123},
                }
            },
        ]

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_sleep)
    client = SimpleNamespace(
        create_session=create_session,
        send_prompt_async=send_prompt,
        messages=messages,
        session_statuses=AsyncMock(return_value={}),
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
    )
    events = SimpleNamespace(publish=AsyncMock())
    dispatcher = QueueDispatcher(
        db, SimpleNamespace(ensure=AsyncMock(return_value=client)), events, 1
    )
    for _ in range(4):
        job = db.claim_next_job(1)
        assert job is not None
        await dispatcher._execute(job)
    assert db.claim_next_job(1) is None
    runs = db.list_task_runs(user["id"])
    assert {job["task_id"] for job in runs} == {root["id"], b["id"], c["id"], d["id"]}
    assert all(job["status"] == "completed" for job in runs)
    assert all(job["user_input"] == run["user_input"] for job in runs)
    assert len(prompts) == 4
    queued_events = [
        call.args[0]["job"]
        for call in events.publish.call_args_list
        if call.args[0]["job"]["status"] == "queued"
    ]
    assert {job["task_id"] for job in queued_events} == {b["id"], c["id"], d["id"]}
