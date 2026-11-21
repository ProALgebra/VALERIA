from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database, utcnow
from opencode_web.vflow.anomalies import ANOMALY_CODES, ITEM_LIMIT, VFlowAnomalies
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "anomaly-flow" {
  version: 1;
  input: json;
  step pass = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> pass -> end
}
"""


def _publish_and_run(
    database: Database,
    *,
    actor: dict,
    workspace,
    slug: str = "anomaly-flow",
    input_data: dict | None = None,
) -> tuple[dict, dict, dict, dict]:
    service = VFlowService(database)
    definition = service.create_definition(actor["id"], slug=slug, title="Anomaly flow")
    service.save_draft(definition["id"], actor["id"], source=SOURCE)
    service.publish(definition["id"], actor["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    run = VFlowRuntime(database).create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=actor["id"],
        workspace_id=str(workspace),
        input_data=input_data if input_data is not None else {"secret": "TOP_SECRET"},
    )
    point = database._one(
        "SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],)
    )
    assert point is not None
    return definition, version, run, point


@pytest.fixture
def anomaly_flow(tmp_path):
    database = Database(tmp_path / "anomalies.sqlite3", "secret")
    database.initialize()
    admin = database.create_user(
        "admin", "a-strong-password", "admin", str(tmp_path)
    )
    definition, version, run, point = _publish_and_run(
        database, actor=admin, workspace=tmp_path
    )
    try:
        yield SimpleNamespace(
            database=database,
            admin=admin,
            definition=definition,
            version=version,
            run=run,
            point=point,
            tmp_path=tmp_path,
        )
    finally:
        database.close()


def _set_point(database: Database, point_id: str, **values) -> None:
    columns = ", ".join(f"{key} = ?" for key in values)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE vflow_points SET {columns} WHERE id = ?",
            (*values.values(), point_id),
        )


def _link_rows(scan: dict, code: str) -> list[dict]:
    return [item for item in scan["items"] if item["code"] == code]


def test_clean_state_is_ok_and_has_no_content(anomaly_flow):
    flow = anomaly_flow
    scan = VFlowAnomalies(flow.database).scan()

    assert scan["status"] == "ok"
    assert set(scan["counts"]) == set(ANOMALY_CODES)
    assert all(count == 0 for count in scan["counts"].values())
    assert scan["items"] == []
    assert "TOP_SECRET" not in json.dumps(scan)


def test_stuck_run_is_reported(anomaly_flow):
    flow = anomaly_flow
    _set_point(flow.database, flow.point["id"], status="consumed")

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["status"] == "degraded"
    assert scan["counts"]["RUN_STUCK"] == 1
    assert _link_rows(scan, "RUN_STUCK") == [
        {
            "code": "RUN_STUCK",
            "run_id": flow.run["id"],
            "point_id": None,
            "node_id": None,
        }
    ]


def test_waiting_task_without_link_or_pending_outbox_is_reported(anomaly_flow):
    flow = anomaly_flow
    _set_point(flow.database, flow.point["id"], status="waiting_task")

    scan = VFlowAnomalies(flow.database).scan()
    assert scan["counts"]["TASK_LINK_MISSING"] == 1
    assert _link_rows(scan, "TASK_LINK_MISSING") == [
        {
            "code": "TASK_LINK_MISSING",
            "run_id": flow.run["id"],
            "point_id": flow.point["id"],
            "node_id": flow.point["node_id"],
        }
    ]

    now = utcnow()
    with flow.database.transaction() as connection:
        connection.execute(
            """INSERT INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json,
                status, attempts, next_attempt_at, created_at)
               VALUES ('pending-enqueue', 'pending-enqueue', ?, ?, 'task_enqueue',
                       '{}', 'pending', 0, ?, ?)""",
            (flow.run["id"], flow.point["id"], now, now),
        )

    pending_scan = VFlowAnomalies(flow.database).scan()
    assert pending_scan["counts"]["TASK_LINK_MISSING"] == 0


def test_orphaned_task_link_is_reported(anomaly_flow):
    flow = anomaly_flow
    now = utcnow()
    with flow.database.transaction() as connection:
        connection.execute(
            """INSERT INTO vflow_task_links
               (id, run_id, point_id, node_id, visit_seq, attempt, task_id,
                task_run_id, enqueue_key, status, queued_at)
               VALUES ('link-orphan', ?, ?, ?, 1, 1, 'task-1', 'missing-job-1',
                       'enqueue-orphan', 'queued', ?)""",
            (flow.run["id"], flow.point["id"], flow.point["node_id"], now),
        )

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["counts"]["TASK_ORPHANED"] == 1
    assert _link_rows(scan, "TASK_ORPHANED") == [
        {
            "code": "TASK_ORPHANED",
            "run_id": flow.run["id"],
            "point_id": flow.point["id"],
            "node_id": flow.point["node_id"],
        }
    ]


def test_open_fork_with_all_arrivals_is_reported_ready(anomaly_flow):
    flow = anomaly_flow
    now = utcnow()
    fork_id = "fork-ready"
    with flow.database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET status = 'forked', updated_at = ? WHERE id = ?",
            (now, flow.point["id"]),
        )
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, created_at)
               VALUES (?, ?, 'split', 'combined', ?, 1, 2, '{}', '[]', 'open', ?)""",
            (fork_id, flow.run["id"], flow.point["id"], now),
        )
        for branch_index in range(2):
            arrival_point_id = f"arrival-{branch_index}"
            frame = {
                "fork_instance_id": fork_id,
                "fork_node_id": "split",
                "merge_node_id": "combined",
                "branch_index": branch_index,
                "expected_count": 2,
            }
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, 'combined', 'waiting_merge', '{}', ?, ?, ?, 1, ?, ?)""",
                (
                    arrival_point_id,
                    flow.run["id"],
                    json.dumps([frame]),
                    flow.point["id"],
                    branch_index + 2,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO vflow_merge_arrivals
                   (id, fork_instance_id, merge_node_id, branch_index, point_id,
                    data_json, arrived_at)
                   VALUES (?, ?, 'combined', ?, ?, '{}', ?)""",
                (
                    f"arrival-row-{branch_index}",
                    fork_id,
                    branch_index,
                    arrival_point_id,
                    now,
                ),
            )

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["counts"]["MERGE_READY"] == 1
    assert _link_rows(scan, "MERGE_READY") == [
        {
            "code": "MERGE_READY",
            "run_id": flow.run["id"],
            "point_id": flow.point["id"],
            "node_id": "combined",
        }
    ]


def test_dead_outbox_is_reported(anomaly_flow):
    flow = anomaly_flow
    now = utcnow()
    with flow.database.transaction() as connection:
        connection.execute(
            """INSERT INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json,
                status, attempts, last_error, next_attempt_at, created_at)
               VALUES ('outbox-dead', 'outbox-dead', ?, ?, 'task_enqueue',
                       '{"prompt": "TOP_SECRET"}', 'dead', 3, 'failed', ?, ?)""",
            (flow.run["id"], flow.point["id"], now, now),
        )

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["counts"]["OUTBOX_DEAD"] == 1
    assert _link_rows(scan, "OUTBOX_DEAD") == [
        {
            "code": "OUTBOX_DEAD",
            "run_id": flow.run["id"],
            "point_id": flow.point["id"],
            "node_id": flow.point["node_id"],
        }
    ]
    assert "TOP_SECRET" not in json.dumps(scan)


def test_active_run_with_missing_handler_is_reported(anomaly_flow):
    flow = anomaly_flow
    with flow.database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_versions SET required_handlers_json = ? WHERE id = ?",
            ('["core.transform@9.9"]', flow.version["id"]),
        )

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["counts"]["HANDLER_MISSING"] == 1
    assert _link_rows(scan, "HANDLER_MISSING") == [
        {
            "code": "HANDLER_MISSING",
            "run_id": flow.run["id"],
            "point_id": None,
            "node_id": None,
        }
    ]


def test_items_are_capped_but_counts_are_exact(anomaly_flow):
    flow = anomaly_flow
    now = utcnow()
    total = ITEM_LIMIT + 5
    with flow.database.transaction() as connection:
        for index in range(total):
            connection.execute(
                """INSERT INTO vflow_outbox
                   (id, dedupe_key, run_id, point_id, kind, payload_json,
                    status, attempts, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, 'task_enqueue', '{}', 'dead', 0, ?, ?)""",
                (
                    f"dead-{index}",
                    f"dead-{index}",
                    flow.run["id"],
                    flow.point["id"],
                    now,
                    now,
                ),
            )

    scan = VFlowAnomalies(flow.database).scan()

    assert scan["counts"]["OUTBOX_DEAD"] == total
    assert len(scan["items"]) == ITEM_LIMIT
    assert [item["code"] for item in scan["items"]] == ["OUTBOX_DEAD"] * ITEM_LIMIT


@pytest.fixture
async def anomaly_app(settings, workspace):
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


async def test_anomaly_endpoint_is_admin_only_and_has_no_internals(anomaly_app):
    t = anomaly_app
    _definition, _version, run, point = _publish_and_run(
        t.database,
        actor=t.worker,
        workspace=t.worker_path,
        slug="endpoint-flow",
    )
    now = utcnow()
    with t.database.transaction() as connection:
        connection.execute(
            """INSERT INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json,
                status, attempts, last_error, next_attempt_at, created_at)
               VALUES ('endpoint-dead', 'endpoint-dead', ?, ?, 'task_enqueue',
                       '{"prompt": "TOP_SECRET"}', 'dead', 1, 'failed', ?, ?)""",
            (run["id"], point["id"], now, now),
        )

    await _login(t.client, "worker")
    denied = await t.client.get("/api/admin/vflow/anomalies")
    assert denied.status_code == 403

    await _login(t.client, "admin")
    response = await t.client.get("/api/admin/vflow/anomalies")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "degraded"
    assert body["counts"]["OUTBOX_DEAD"] == 1
    assert body["items"] == [
        {
            "code": "OUTBOX_DEAD",
            "run_id": run["id"],
            "point_id": point["id"],
            "node_id": point["node_id"],
        }
    ]
    text = response.text
    for marker in (
        "TOP_SECRET",
        "Do not expose this",
        "input_json",
        "graph_ir_json",
        "vflow_source",
        "task_snapshots_json",
        "payload_json",
    ):
        assert marker not in text
