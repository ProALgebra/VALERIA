from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database, utcnow
from opencode_web.vflow.health import VFlowHealth
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "health-flow" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""


def _admin(database: Database, workspace, login: str = "health-admin") -> dict:
    return database.create_user(
        login, "a-strong-password", "admin", str(workspace)
    )


def _publish(database: Database, actor_id: str, *, slug: str = "health-flow") -> tuple:
    service = VFlowService(database)
    definition = service.create_definition(
        actor_id, slug=slug, title="Health flow"
    )
    service.save_draft(definition["id"], actor_id, source=SOURCE)
    service.publish(definition["id"], actor_id)
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def _active_run(
    database: Database,
    definition: dict,
    version: dict,
    user: dict,
    workspace,
) -> dict:
    runtime = VFlowRuntime(database)
    return runtime.create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=user["id"],
        workspace_id=str(workspace),
        input_data={"secret": "TOP_SECRET", "prompt": "Do not expose this"},
    )


def test_health_empty_database_is_ok(tmp_path):
    database = Database(tmp_path / "health.sqlite3", "secret")
    database.initialize()
    try:
        snapshot = VFlowHealth(database).snapshot()
        assert snapshot["status"] == "ok"
        assert snapshot["checks"]["database"]["status"] == "ok"
        assert snapshot["checks"]["handlers"]["status"] == "ok"
        assert snapshot["checks"]["handlers"]["active_runs_checked"] == 0
        assert snapshot["counts"] == {
            "active_runs": 0,
            "active_points": 0,
            "open_forks": 0,
            "expired_leases": 0,
            "outbox_pending": 0,
            "outbox_processing": 0,
        }
    finally:
        database.close()


def test_health_active_run_with_known_handlers_is_ok(tmp_path):
    database = Database(tmp_path / "health.sqlite3", "secret")
    database.initialize()
    try:
        admin = _admin(database, tmp_path)
        definition, version = _publish(database, admin["id"])
        run = _active_run(database, definition, version, admin, tmp_path)

        snapshot = VFlowHealth(database).snapshot()
        assert run["status"] == "running"
        assert snapshot["status"] == "ok"
        assert snapshot["checks"]["handlers"]["active_runs_checked"] == 1
        assert snapshot["checks"]["handlers"]["incompatible_versions"] == []
        assert snapshot["counts"]["active_runs"] == 1
        assert snapshot["counts"]["active_points"] == 1
        assert "TOP_SECRET" not in json.dumps(snapshot)
    finally:
        database.close()


def test_health_active_run_with_unknown_handler_is_degraded(tmp_path):
    database = Database(tmp_path / "health.sqlite3", "secret")
    database.initialize()
    try:
        admin = _admin(database, tmp_path)
        definition, version = _publish(database, admin["id"])
        run = _active_run(database, definition, version, admin, tmp_path)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_versions SET required_handlers_json = ? WHERE id = ?",
                ('["core.transform@9.9"]', version["id"]),
            )

        snapshot = VFlowHealth(database).snapshot()
        assert run["status"] == "running"
        assert snapshot["status"] == "degraded"
        handlers = snapshot["checks"]["handlers"]
        assert handlers["status"] == "degraded"
        assert handlers["active_runs_checked"] == 1
        assert handlers["incompatible_versions"] == [
            {
                "version_id": version["id"],
                "run_count": 1,
                "missing_handlers": ["core.transform@9.9"],
            }
        ]
        assert "TOP_SECRET" not in json.dumps(snapshot)
    finally:
        database.close()


def test_health_operational_counts_are_bounded_aggregates(tmp_path):
    database = Database(tmp_path / "health.sqlite3", "secret")
    database.initialize()
    try:
        admin = _admin(database, tmp_path)
        definition, version = _publish(database, admin["id"])
        run = _active_run(database, definition, version, admin, tmp_path)
        point = database._one(
            "SELECT id FROM vflow_points WHERE run_id = ?", (run["id"],)
        )
        assert point is not None
        now = utcnow()
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'claimed', lease_owner = 'worker',
                       lease_token = 'lease-token', lease_until = ?
                   WHERE id = ?""",
                ("2000-01-01T00:00:00+00:00", point["id"]),
            )
            connection.execute(
                """INSERT INTO vflow_fork_instances
                   (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                    parent_visit_seq, expected_count, source_data_json,
                    parent_fork_stack_json, status, created_at)
                   VALUES ('fork-1', ?, 'fork', 'merge', ?, 1, 2, '{}', '[]',
                           'open', ?)""",
                (run["id"], point["id"], now),
            )
            for outbox_id, status in (
                ("outbox-pending", "pending"),
                ("outbox-processing", "processing"),
            ):
                connection.execute(
                    """INSERT INTO vflow_outbox
                       (id, dedupe_key, run_id, point_id, kind, payload_json,
                        status, attempts, next_attempt_at, created_at)
                       VALUES (?, ?, ?, ?, 'task_enqueue', '{}', ?, 0, ?, ?)""",
                    (
                        outbox_id,
                        outbox_id,
                        run["id"],
                        point["id"],
                        status,
                        now,
                        now,
                    ),
                )

        snapshot = VFlowHealth(database).snapshot()
        assert snapshot["status"] == "ok"
        assert snapshot["counts"] == {
            "active_runs": 1,
            "active_points": 1,
            "open_forks": 1,
            "expired_leases": 1,
            "outbox_pending": 1,
            "outbox_processing": 1,
        }
    finally:
        database.close()


def test_health_missing_required_table_is_error(tmp_path):
    database = Database(tmp_path / "health.sqlite3", "secret")
    database.initialize()
    try:
        with database.transaction() as connection:
            connection.execute("DROP TABLE vflow_outbox")
        snapshot = VFlowHealth(database).snapshot()
        assert snapshot["status"] == "error"
        assert snapshot["checks"]["database"]["status"] == "error"
        assert "vflow_outbox" in snapshot["checks"]["database"]["missing_tables"]
        assert snapshot["checks"]["handlers"]["status"] == "skipped"
    finally:
        database.close()


@pytest.fixture
async def health_app(settings, workspace):
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
                workspace=workspace,
                admin=admin,
                worker=worker,
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


async def test_health_endpoint_is_admin_only_and_has_no_internals(health_app):
    t = health_app
    definition, version = _publish(t.database, t.admin["id"])
    run = _active_run(
        t.database, definition, version, t.worker, t.workspace / "worker"
    )
    assert run["status"] == "running"

    await _login(t.client, "worker")
    denied = await t.client.get("/api/admin/vflow/health")
    assert denied.status_code == 403

    await _login(t.client, "admin")
    response = await t.client.get("/api/admin/vflow/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["counts"]["active_runs"] == 1
    assert body["checks"]["handlers"]["active_runs_checked"] == 1
    assert t.app.state.vflow_health["status"] == "ok"

    text = response.text
    for marker in (
        "TOP_SECRET",
        "Do not expose this",
        "input_json",
        "graph_ir_json",
        "vflow_source",
        "task_snapshots_json",
    ):
        assert marker not in text
