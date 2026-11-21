from __future__ import annotations

import pytest

from opencode_web.database import Database
from opencode_web.vflow.gateway import ValeriaTaskGateway, VFlowTaskError


def make_db(tmp_path):
    database = Database(tmp_path / "vflow-gateway.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", None)
    worker = database.create_user(
        "worker", "a-strong-password", "user", str(tmp_path)
    )
    return database, admin, worker


def create_task(database, admin, *, published=True, next_task_ids=None):
    return database.create_task(
        admin["id"],
        "Echo",
        "Echo the input",
        "Return the input.",
        "build",
        None,
        None,
        None,
        published,
        next_task_ids=next_task_ids,
    )


def enqueue(gateway, task, worker, text="hello", task_snapshot=None):
    return gateway.enqueue(
        run_id="run-1",
        point_id="point-1",
        task_id=task["id"],
        user_id=worker["id"],
        workspace_id=worker["workspace_path"],
        input_data={"text": text},
        result_mode="auto",
        task_snapshot=task_snapshot,
    )


def snapshot_of(task):
    return {
        "task_id": task["id"],
        "title": task["title"],
        "updated_at": task["updated_at"],
        "agent": task["agent"],
        "provider_id": task["provider_id"],
        "model_id": task["model_id"],
        "model_variant": task["model_variant"],
        "queue_id": task["queue_id"],
    }


def test_gateway_creates_queue_job_and_reads_result(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        task = create_task(database, admin)
        gateway = ValeriaTaskGateway(database)
        job_id = enqueue(gateway, task, worker)

        job = database.get_job(job_id)
        assert job["status"] == "queued"
        assert job["pipeline_json"] == ""
        assert "vflow_input" in job["user_input"]
        assert gateway.status(job_id)["status"] == "queued"

        database.complete_task_run(job_id, {"message_id": "m-1", "text": "done"})
        assert gateway.status(job_id) == {"status": "completed", "text": "done"}
    finally:
        database.close()


def test_gateway_allows_internal_task_and_suppresses_legacy_children(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        child = create_task(database, admin)
        parent = create_task(database, admin, published=False, next_task_ids=[child["id"]])
        gateway = ValeriaTaskGateway(database)
        job_id = enqueue(gateway, parent, worker)

        assert database.get_job(job_id)["pipeline_json"] == ""
        database.complete_task_run(job_id, {"message_id": "m-1", "text": "done"})
        children = database._all(
            "SELECT id FROM queue_jobs WHERE parent_job_id = ?", (job_id,)
        )
        assert children == []
    finally:
        database.close()


def test_same_point_enqueue_is_idempotent(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        task = create_task(database, admin)
        gateway = ValeriaTaskGateway(database)
        first = enqueue(gateway, task, worker)
        second = enqueue(gateway, task, worker)
        assert first == second
        jobs = database._one(
            "SELECT COUNT(*) AS count FROM queue_jobs WHERE message_id LIKE 'msg_vflow_%'"
        )
        chats = database._one("SELECT COUNT(*) AS count FROM chats")
        assert jobs["count"] == 1
        assert chats["count"] == 1
        assert database.get_job(first)["message_id"].startswith("msg_vflow_")
        assert database.get_job(first)["pipeline_json"] == ""
    finally:
        database.close()


def test_gateway_cancels_queued_job(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        task = create_task(database, admin)
        gateway = ValeriaTaskGateway(database)
        job_id = enqueue(gateway, task, worker)

        assert gateway.cancel(job_id) is True
        assert gateway.status(job_id)["status"] == "cancelled"
    finally:
        database.close()


def test_gateway_accepts_matching_task_snapshot(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        task = create_task(database, admin)
        gateway = ValeriaTaskGateway(database)
        job_id = enqueue(gateway, task, worker, task_snapshot=snapshot_of(task))

        assert database.get_job(job_id)["status"] == "queued"
    finally:
        database.close()


def test_gateway_rejects_changed_task_snapshot_without_queue_job(tmp_path):
    database, admin, worker = make_db(tmp_path)
    try:
        task = create_task(database, admin)
        snapshot = snapshot_of(task)
        updated = database.update_task(task["id"], {"content": "Changed"})
        assert updated is not None
        assert updated["updated_at"] != snapshot["updated_at"]

        gateway = ValeriaTaskGateway(database)
        with pytest.raises(VFlowTaskError) as error:
            enqueue(gateway, task, worker, task_snapshot=snapshot)
        assert error.value.code == "TASK_CHANGED"
        assert database._one("SELECT COUNT(*) AS count FROM queue_jobs")["count"] == 0
        assert database._one("SELECT COUNT(*) AS count FROM chats")["count"] == 0
    finally:
        database.close()
