from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database
from opencode_web.vflow.retention import VFlowRetention, VFlowRetentionScheduler
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "retention" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""

NOW = datetime(2026, 3, 1, tzinfo=UTC)
OLD = "2025-12-01T00:00:00+00:00"
RECENT = "2026-02-20T00:00:00+00:00"
ZERO_COUNTS = {
    "runs": 0,
    "points": 0,
    "forks": 0,
    "arrivals": 0,
    "task_links": 0,
    "events": 0,
    "outbox": 0,
}


def make_store(tmp_path):
    database = Database(tmp_path / "retention.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    service = VFlowService(database)
    definition = service.create_definition(
        admin["id"], slug="retention-flow", title="Retention flow"
    )
    service.save_draft(definition["id"], admin["id"], source=SOURCE)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    return database, admin, definition, version


def add_run(
    database,
    admin,
    definition,
    version,
    *,
    run_id: str,
    status: str,
    created_at: str,
    finished_at: str | None = None,
    dependents: bool = False,
) -> None:
    started_at = None if status == "created" else created_at
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO vflow_runs
               (id, definition_id, version_id, started_by_user_id, workspace_id,
                status, input_json, created_at, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
            (
                run_id,
                definition["id"],
                version["id"],
                admin["id"],
                "/workspace",
                status,
                created_at,
                started_at,
                finished_at,
            ),
        )
        if not dependents:
            return
        first_point = f"{run_id}-point-1"
        second_point = f"{run_id}-point-2"
        for point_id, visit_seq, parent_point_id in (
            (first_point, 1, None),
            (second_point, 2, first_point),
        ):
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, 'pass', 'consumed', '{}', '[]', ?, ?, 1, ?, ?)""",
                (
                    point_id,
                    run_id,
                    parent_point_id,
                    visit_seq,
                    created_at,
                    created_at,
                ),
            )
        fork_id = f"{run_id}-fork"
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, continuation_point_id, created_at)
               VALUES (?, ?, 'split', 'merge', ?, 1, 2, '{}', '[]', 'merged', ?, ?)""",
            (fork_id, run_id, first_point, second_point, created_at),
        )
        connection.execute(
            """INSERT INTO vflow_merge_arrivals
               (id, fork_instance_id, merge_node_id, branch_index, point_id,
                data_json, arrived_at)
               VALUES (?, ?, 'merge', 0, ?, '{}', ?)""",
            (f"{run_id}-arrival", fork_id, second_point, created_at),
        )
        connection.execute(
            """INSERT INTO vflow_task_links
               (id, run_id, point_id, node_id, visit_seq, attempt, task_id,
                task_run_id, enqueue_key, status, queued_at)
               VALUES (?, ?, ?, 'pass', 1, 1, 'task-1', ?, ?, 'finished', ?)""",
            (
                f"{run_id}-link",
                run_id,
                first_point,
                f"{run_id}-task-run",
                f"{run_id}-enqueue",
                created_at,
            ),
        )
        connection.execute(
            """INSERT INTO vflow_events
               (run_id, point_id, node_id, event_type, payload_json, created_at)
               VALUES (?, ?, 'pass', 'RUN_STARTED', '{}', ?)""",
            (run_id, first_point, created_at),
        )
        connection.execute(
            """INSERT INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json, status,
                attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, 'task_enqueue', '{}', 'sent', 1, ?, ?)""",
            (
                f"{run_id}-outbox",
                f"{run_id}-dedupe",
                run_id,
                first_point,
                created_at,
                created_at,
            ),
        )


def run_count(database, run_id: str) -> int:
    row = database._one("SELECT COUNT(*) AS count FROM vflow_runs WHERE id = ?", (run_id,))
    return int(row["count"]) if row else 0


def dependent_counts(database, run_id: str) -> dict[str, int]:
    return {
        "points": int(
            database._one(
                "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?", (run_id,)
            )["count"]
        ),
        "forks": int(
            database._one(
                "SELECT COUNT(*) AS count FROM vflow_fork_instances WHERE run_id = ?",
                (run_id,),
            )["count"]
        ),
        "task_links": int(
            database._one(
                "SELECT COUNT(*) AS count FROM vflow_task_links WHERE run_id = ?",
                (run_id,),
            )["count"]
        ),
        "events": int(
            database._one(
                "SELECT COUNT(*) AS count FROM vflow_events WHERE run_id = ?", (run_id,)
            )["count"]
        ),
        "outbox": int(
            database._one(
                "SELECT COUNT(*) AS count FROM vflow_outbox WHERE run_id = ?", (run_id,)
            )["count"]
        ),
    }


def test_old_terminal_run_deleted_with_dependents_and_definitions_preserved(tmp_path):
    database, admin, definition, version = make_store(tmp_path)
    try:
        add_run(
            database,
            admin,
            definition,
            version,
            run_id="old-succeeded",
            status="succeeded",
            created_at=OLD,
            finished_at=OLD,
            dependents=True,
        )
        add_run(
            database,
            admin,
            definition,
            version,
            run_id="recent-succeeded",
            status="succeeded",
            created_at=RECENT,
            finished_at=RECENT,
            dependents=True,
        )
        for run_id, status in (
            ("old-created", "created"),
            ("old-running", "running"),
            ("old-cancel-requested", "cancel_requested"),
        ):
            add_run(
                database,
                admin,
                definition,
                version,
                run_id=run_id,
                status=status,
                created_at=OLD,
            )

        counts = VFlowRetention(database, 30).run_once(now=NOW)
        assert counts == {
            "runs": 1,
            "points": 2,
            "forks": 1,
            "arrivals": 1,
            "task_links": 1,
            "events": 1,
            "outbox": 1,
        }

        assert run_count(database, "old-succeeded") == 0
        assert dependent_counts(database, "old-succeeded") == {
            "points": 0,
            "forks": 0,
            "task_links": 0,
            "events": 0,
            "outbox": 0,
        }
        assert run_count(database, "recent-succeeded") == 1
        assert dependent_counts(database, "recent-succeeded")["points"] == 2
        for run_id in ("old-created", "old-running", "old-cancel-requested"):
            assert run_count(database, run_id) == 1

        assert database._one(
            "SELECT id FROM vflow_definitions WHERE id = ?", (definition["id"],)
        )
        assert database._one(
            "SELECT id FROM vflow_versions WHERE id = ?", (version["id"],)
        )
        assert database._all("PRAGMA foreign_key_check") == []
    finally:
        database.close()


def test_failed_and_cancelled_runs_expire_but_active_runs_are_preserved(tmp_path):
    database, admin, definition, version = make_store(tmp_path)
    try:
        for run_id, status in (
            ("old-failed", "failed"),
            ("old-cancelled", "cancelled"),
        ):
            add_run(
                database,
                admin,
                definition,
                version,
                run_id=run_id,
                status=status,
                created_at=OLD,
                finished_at=None if status == "cancelled" else OLD,
                dependents=True,
            )
        for run_id, status in (
            ("old-created", "created"),
            ("old-running", "running"),
            ("old-cancel-requested", "cancel_requested"),
        ):
            add_run(
                database,
                admin,
                definition,
                version,
                run_id=run_id,
                status=status,
                created_at=OLD,
            )

        counts = VFlowRetention(database, 30).run_once(now=NOW)
        assert counts["runs"] == 2
        assert counts["points"] == 4
        assert counts["forks"] == 2
        assert counts["arrivals"] == 2
        assert run_count(database, "old-failed") == 0
        assert run_count(database, "old-cancelled") == 0
        for run_id in ("old-created", "old-running", "old-cancel-requested"):
            assert run_count(database, run_id) == 1
    finally:
        database.close()


def test_dry_run_keeps_rows_and_second_run_is_idempotent(tmp_path):
    database, admin, definition, version = make_store(tmp_path)
    try:
        add_run(
            database,
            admin,
            definition,
            version,
            run_id="old-succeeded",
            status="succeeded",
            created_at=OLD,
            finished_at=OLD,
            dependents=True,
        )
        retention = VFlowRetention(database, 30)

        dry_counts = retention.run_once(now=NOW, dry_run=True)
        assert dry_counts["runs"] == 1
        assert dry_counts["points"] == 2
        assert run_count(database, "old-succeeded") == 1
        assert dependent_counts(database, "old-succeeded")["points"] == 2

        counts = retention.run_once(now=NOW)
        assert counts == dry_counts
        assert run_count(database, "old-succeeded") == 0

        assert retention.run_once(now=NOW) == ZERO_COUNTS
        assert retention.run_once(now=NOW, dry_run=True) == ZERO_COUNTS
    finally:
        database.close()


def test_retention_rejects_invalid_days() -> None:
    with pytest.raises(ValueError, match="retention_days"):
        VFlowRetention(object(), 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="retention_days"):
        VFlowRetention(object(), True)  # type: ignore[arg-type]


class FakeRetention:
    def __init__(self) -> None:
        self.calls = 0
        self.dry_runs = 0

    def run_once(self, now=None, dry_run: bool = False) -> dict[str, int]:
        del now
        self.calls += 1
        if dry_run:
            self.dry_runs += 1
        return dict(ZERO_COUNTS)


async def wait_for_calls(retention: FakeRetention, count: int, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while retention.calls < count:
        if loop.time() >= deadline:
            raise AssertionError(f"expected {count} calls, got {retention.calls}")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_retention_scheduler_runs_on_start_and_stops_cleanly() -> None:
    retention = FakeRetention()
    scheduler = VFlowRetentionScheduler(retention, interval_seconds=0.01)

    assert await scheduler.run_once(dry_run=True) == ZERO_COUNTS
    assert retention.dry_runs == 1
    assert retention.calls == 1

    await scheduler.start()
    await scheduler.start()
    await wait_for_calls(retention, 2)
    await scheduler.stop()
    calls = retention.calls

    await scheduler.stop()
    await asyncio.sleep(0.03)
    assert retention.calls == calls


@pytest.fixture
async def retention_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        database = app.state.database
        worker_path = workspace / "worker"
        worker_path.mkdir()
        worker = database.create_user(
            "worker", "a-strong-password", "user", str(worker_path)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(
                app=app, client=client, db=database, worker=worker
            )


async def login(client, name: str) -> None:
    password = (
        "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    )
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


async def test_retention_admin_endpoints_are_admin_only(retention_app):
    t = retention_app
    admin = t.db.get_user_by_login("admin")
    service = VFlowService(t.db)
    definition = service.create_definition(
        admin["id"], slug="retention-api", title="Retention API"
    )
    service.save_draft(definition["id"], admin["id"], source=SOURCE)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    add_run(
        t.db,
        admin,
        definition,
        version,
        run_id="old-succeeded",
        status="succeeded",
        created_at=OLD,
        finished_at=OLD,
        dependents=True,
    )

    await login(t.client, "worker")
    denied = await t.client.post("/api/admin/vflow/retention/run", json={"dry_run": True})
    assert denied.status_code == 403
    assert (await t.client.get("/api/admin/vflow/retention")).status_code == 403

    await login(t.client, "admin")
    stats = await t.client.get("/api/admin/vflow/retention")
    assert stats.status_code == 200, stats.text
    assert stats.json()["retention_days"] == 30
    assert stats.json()["eligible"]["runs"] == 1
    assert stats.json()["active_runs"] == 0

    preview = await t.client.post(
        "/api/admin/vflow/retention/run", json={"dry_run": True}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["runs"] == 1
    assert preview.json()["points"] == 2
    assert run_count(t.db, "old-succeeded") == 1

    result = await t.client.post("/api/admin/vflow/retention/run")
    assert result.status_code == 200, result.text
    assert result.json()["runs"] == 1
    assert run_count(t.db, "old-succeeded") == 0
    assert (await t.client.get("/api/admin/vflow/retention")).json()["eligible"][
        "runs"
    ] == 0
