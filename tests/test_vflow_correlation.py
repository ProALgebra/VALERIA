from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database
from opencode_web.vflow.gateway import ValeriaTaskGateway
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

TASK_SOURCE = """\
workflow "task-correlation" {
  version: 1;
  input: json;
  step work = task.run(task_id: "<TASK_ID>", input_path: "$", result_mode: "auto");
  start -> work -> end
}
"""


def _create_task(database: Database, admin: dict) -> dict:
    return database.create_task(
        admin_id=admin["id"],
        title="Correlated task",
        description="",
        content="Private task prompt that must not leak",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
        published=True,
    )


def _publish_task_pipeline(
    database: Database, admin: dict, task: dict, *, slug: str = "task-correlation"
) -> tuple[dict, dict]:
    service = VFlowService(database)
    definition = service.create_definition(admin["id"], slug=slug, title="Correlation")
    service.save_draft(
        definition["id"],
        admin["id"],
        source=TASK_SOURCE.replace("<TASK_ID>", task["id"]),
    )
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def test_gateway_persists_vflow_correlation_on_queue_job(tmp_path):
    database = Database(tmp_path / "correlation.sqlite3", "secret")
    database.initialize()
    try:
        admin = database.create_user(
            "admin", "a-strong-password", "admin", str(tmp_path)
        )
        worker = database.create_user(
            "worker", "a-strong-password", "user", str(tmp_path)
        )
        task = _create_task(database, admin)
        job_id = ValeriaTaskGateway(database).enqueue(
            run_id="run-correlation",
            point_id="point-correlation",
            node_id="work",
            task_id=task["id"],
            user_id=worker["id"],
            workspace_id=worker["workspace_path"],
            input_data={"name": "Alice"},
            result_mode="auto",
        )

        job = database.get_job(job_id)
        assert job["vflow_run_id"] == "run-correlation"
        assert job["vflow_point_id"] == "point-correlation"
        assert job["origin_node_id"] == "work"
        # OpenCode v2 rejects prompt IDs that do not start with ``msg_``.
        assert job["message_id"].startswith("msg_vflow_")
    finally:
        database.close()


class RecordingGateway:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return "job-1"

    def status(self, task_run_id: str):
        return {"status": "completed", "text": "{}"}

    def cancel(self, task_run_id: str) -> None:
        return None


def test_runtime_passes_point_node_id_to_gateway(tmp_path):
    database = Database(tmp_path / "runtime-correlation.sqlite3", "secret")
    database.initialize()
    try:
        admin = database.create_user(
            "admin", "a-strong-password", "admin", str(tmp_path)
        )
        task = _create_task(database, admin)
        definition, version = _publish_task_pipeline(database, admin, task)
        gateway = RecordingGateway()
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"name": "Alice"},
        )
        runtime.run_until_idle(run["id"])

        assert gateway.enqueued[0]["node_id"] == "work"
        assert gateway.enqueued[0]["run_id"] == run["id"]
        point = database._one(
            "SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],)
        )
        assert gateway.enqueued[0]["point_id"] == point["id"]
    finally:
        database.close()


def test_queue_job_correlation_migration_is_idempotent(tmp_path):
    database = Database(tmp_path / "migration.sqlite3", "secret")
    database.initialize()
    try:
        admin = database.create_user(
            "admin", "a-strong-password", "admin", str(tmp_path)
        )
        worker = database.create_user(
            "worker", "a-strong-password", "user", str(tmp_path)
        )
        task = _create_task(database, admin)
        job_id = database.create_task_job(
            task=task,
            user=worker,
            actor_user_id=worker["id"],
            session_id=None,
            user_input="hello",
            max_queued=10,
        )["id"]

        with database.transaction() as connection:
            for column in ("vflow_run_id", "vflow_point_id", "origin_node_id"):
                connection.execute(f"ALTER TABLE queue_jobs DROP COLUMN {column}")

        database.initialize()
        database.initialize()

        columns = {
            row["name"]
            for row in database._all("PRAGMA table_info(queue_jobs)")
        }
        assert {"vflow_run_id", "vflow_point_id", "origin_node_id"} <= columns
        job = database.get_job(job_id)
        assert job["content"].startswith("Private task prompt")
        assert job["vflow_run_id"] is None
        assert job["vflow_point_id"] is None
        assert job["origin_node_id"] is None
    finally:
        database.close()


@pytest.fixture
async def correlation_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        database = app.state.database
        admin = database.get_user_by_login("admin")
        assert admin is not None
        worker_path = workspace / "worker"
        worker_path.mkdir()
        worker = database.create_user(
            "worker", "a-strong-password", "user", str(worker_path)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                database=database,
                admin=admin,
                worker=worker,
                worker_path=worker_path,
            )


async def _login(client, name: str) -> None:
    password = (
        "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    )
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


async def test_admin_run_detail_includes_correlation_but_user_view_does_not(
    correlation_app,
):
    t = correlation_app
    task = _create_task(t.database, t.admin)
    _publish_task_pipeline(t.database, t.admin, task)

    await _login(t.client, "worker")
    created = await t.client.post(
        "/api/pipelines/task-correlation/runs",
        json={"input": {"name": "Alice"}},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    assert "task_correlations" not in created.json()

    finished = t.app.state.vflow_runtime.run_until_idle(run_id)
    assert finished["status"] == "running", finished

    link = t.database._one(
        "SELECT * FROM vflow_task_links WHERE run_id = ?", (run_id,)
    )
    assert link is not None
    job = t.database.get_job(link["task_run_id"])
    assert job["vflow_run_id"] == run_id
    assert job["vflow_point_id"] == link["point_id"]
    assert job["origin_node_id"] == "work"

    user_detail = await t.client.get(f"/api/pipeline-runs/{run_id}")
    assert user_detail.status_code == 200, user_detail.text
    assert "task_correlations" not in user_detail.json()
    assert link["task_run_id"] not in user_detail.text

    await _login(t.client, "admin")
    admin_detail = await t.client.get(f"/api/admin/vflow/runs/{run_id}")
    assert admin_detail.status_code == 200, admin_detail.text
    body = admin_detail.json()
    assert body["task_correlations"] == [
        {
            "point_id": link["point_id"],
            "node_id": "work",
            "origin_node_id": "work",
            "task_id": task["id"],
            "task_run_id": link["task_run_id"],
            "visit_seq": 1,
            "attempt": 1,
            "status": "queued",
            "queued_at": link["queued_at"],
            "started_at": None,
            "finished_at": None,
        }
    ]
    assert "Private task prompt that must not leak" not in admin_detail.text
