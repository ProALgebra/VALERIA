from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from opencode_web.database import Database, utcnow
from opencode_web.vflow.recovery import VFlowRecovery
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

LOAD_FORK_SOURCE = """\
workflow "load-fork" {
  version: 1;
  input: json;
  step split = fork(count: 4, join: combined);
  step branch0 = transform(operations: [{op: "set", path: "$.branch0", value: 0}]);
  step branch1 = transform(operations: [{op: "set", path: "$.branch1", value: 1}]);
  step branch2 = transform(operations: [{op: "set", path: "$.branch2", value: 2}]);
  step branch3 = transform(operations: [{op: "set", path: "$.branch3", value: 3}]);
  step combined = merge();
  step after = transform(operations: [{op: "set", path: "$.after", value: true}]);
  start -> split;
  split[0] -> branch0 -> combined;
  split[1] -> branch1 -> combined;
  split[2] -> branch2 -> combined;
  split[3] -> branch3 -> combined;
  combined -> after -> end
}
"""

PASS_SOURCE = """\
workflow "pass" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""

RECOVERY_FORK_SOURCE = """\
workflow "recovery-fork" {
  version: 1;
  input: json;
  step split = fork(count: 3, join: combined);
  step combined = merge();
  step after = transform(operations: [{op: "set", path: "$.after", value: true}]);
  start -> split;
  split[0] -> combined;
  split[1] -> combined;
  split[2] -> combined;
  combined -> after -> end
}
"""

TASK_SOURCE = """\
workflow "load-task" {
  version: 1;
  input: json;
  step work = task.run(
    task_id: "<TASK_ID>",
    input_path: "$",
    result_mode: "strict_json"
  );
  start -> work -> end
}
"""


class IdempotentGateway:
    """In-memory gateway which deduplicates by idempotency key."""

    def __init__(self, status="completed", text='{"ok": true}'):
        self.current_status = status
        self.text = text
        self.calls = 0
        self.enqueued: list[dict] = []
        self.task_runs: dict[str, str] = {}
        self.cancelled: list[str] = []

    def enqueue(self, **kwargs):
        self.calls += 1
        self.enqueued.append(kwargs)
        key = kwargs.get("idempotency_key") or (
            f"{kwargs['run_id']}:{kwargs['point_id']}"
        )
        if key not in self.task_runs:
            self.task_runs[key] = f"job-{len(self.task_runs) + 1}"
        return self.task_runs[key]

    def status(self, task_run_id: str):
        return {"status": self.current_status, "text": self.text}

    def cancel(self, task_run_id: str) -> bool:
        self.cancelled.append(task_run_id)
        return True


def make_service(tmp_path, name):
    database = Database(tmp_path / f"{name}.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    return database, admin, VFlowService(database)


def publish(service, admin, slug, source):
    definition = service.create_definition(admin["id"], slug=slug, title=slug)
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def start_run(runtime, definition, version, admin, tmp_path, input_data):
    return runtime.create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=admin["id"],
        workspace_id=str(tmp_path / "workspace"),
        input_data=input_data,
    )


def counts_by_run(database, claimed):
    counts: dict[str, int] = {}
    for point_id, _token in claimed:
        row = database._one("SELECT run_id FROM vflow_points WHERE id = ?", (point_id,))
        assert row is not None
        counts[row["run_id"]] = counts.get(row["run_id"], 0) + 1
    return counts


def force_due(database):
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
            (utcnow(),),
        )


def insert_completed_fork(database, run, point, prefix, expected_count=3):
    """Persist the state left by a crash after every branch reached its Merge."""
    now = utcnow()
    fork_id = f"{prefix}-fork"
    source_data = {"seed": prefix}
    arrivals = [{"branch": index} for index in range(expected_count)]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET status = 'forked', updated_at = ? WHERE id = ?",
            (now, point["id"]),
        )
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, created_at)
               VALUES (?, ?, 'split', 'combined', ?, ?, ?, ?, '[]', 'open', ?)""",
            (
                fork_id,
                run["id"],
                point["id"],
                point["visit_seq"],
                expected_count,
                json.dumps(source_data),
                now,
            ),
        )
        for branch_index, data in enumerate(arrivals):
            arrival_point_id = f"{prefix}-arrival-{branch_index}"
            frame = {
                "fork_instance_id": fork_id,
                "fork_node_id": "split",
                "merge_node_id": "combined",
                "branch_index": branch_index,
                "expected_count": expected_count,
            }
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, 'combined', 'waiting_merge', ?, ?, ?, ?, 1, ?, ?)""",
                (
                    arrival_point_id,
                    run["id"],
                    json.dumps(data),
                    json.dumps([frame]),
                    point["id"],
                    branch_index + 2,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO vflow_merge_arrivals
                   (id, fork_instance_id, merge_node_id, branch_index, point_id,
                    data_json, arrived_at)
                   VALUES (?, ?, 'combined', ?, ?, ?, ?)""",
                (
                    f"{prefix}-arrival-row-{branch_index}",
                    fork_id,
                    branch_index,
                    arrival_point_id,
                    json.dumps(data),
                    now,
                ),
            )
    return fork_id


def test_many_runs_claim_and_process_without_duplicate_continuations(tmp_path):
    database, admin, service = make_service(tmp_path, "load")
    try:
        definition, version = publish(
            service, admin, "load-fork", LOAD_FORK_SOURCE
        )
        runtime = VFlowRuntime(database)
        runs = [
            start_run(runtime, definition, version, admin, tmp_path, {"seed": index})
            for index in range(6)
        ]

        rounds = 0
        while any(
            runtime.get_run(run["id"])["status"] == "running" for run in runs
        ):
            rounds += 1
            assert rounds < 100, "load run did not converge"
            first = runtime.claim_due(limit=9, per_run_limit=3)
            second = runtime.claim_due(limit=9, per_run_limit=3)
            first_ids = {point_id for point_id, _token in first}
            second_ids = {point_id for point_id, _token in second}
            # A lease is exclusive: a second claim cannot take the same point
            # while the first claim is still active.
            assert first_ids.isdisjoint(second_ids)

            batch = [*first, *second]
            assert len({point_id for point_id, _token in batch}) == len(batch)
            assert batch, "active runs must have claimable points"

            for point_id, token in batch:
                claimed_row = database._one(
                    "SELECT status, lease_token FROM vflow_points WHERE id = ?",
                    (point_id,),
                )
                assert claimed_row == {"status": "claimed", "lease_token": token}
                assert runtime.process_point(point_id, token) is True
            assert all(
                database._one(
                    "SELECT id FROM vflow_points WHERE lease_token = ?", (token,)
                )
                is None
                for _point_id, token in batch
            )

        for index, run in enumerate(runs):
            finished = runtime.get_run(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert result["source"] == {"seed": index}
            assert [branch["index"] for branch in result["branches"]] == [
                0, 1, 2, 3,
            ]
            assert result["after"] is True

            events = database._all(
                """SELECT event_type, COUNT(*) AS count FROM vflow_events
                   WHERE run_id = ? AND event_type IN
                     ('FORK_SPAWNED', 'MERGE_COMPLETED', 'RUN_SUCCEEDED')
                   GROUP BY event_type""",
                (run["id"],),
            )
            assert {row["event_type"]: row["count"] for row in events} == {
                "FORK_SPAWNED": 1,
                "MERGE_COMPLETED": 1,
                "RUN_SUCCEEDED": 1,
            }
            after_points = database._all(
                "SELECT id FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                (run["id"],),
            )
            assert len(after_points) == 1
            # One entry/Fork row, four branch/Merge arrival rows and exactly
            # one continuation. A duplicate Merge continuation would add one.
            assert database._one(
                "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
                (run["id"],),
            )["count"] == 6
            assert finished["point_count_total"] == 6
            active = database._one(
                """SELECT COUNT(*) AS count FROM vflow_points
                   WHERE run_id = ? AND status IN
                     ('ready', 'claimed', 'waiting_task', 'waiting_merge')""",
                (run["id"],),
            )
            assert active["count"] == 0
    finally:
        database.close()


def test_scheduler_fairness_under_several_active_runs(tmp_path):
    database, admin, service = make_service(tmp_path, "fair-load")
    try:
        definition, version = publish(
            service, admin, "fair-load", LOAD_FORK_SOURCE
        )
        runtime = VFlowRuntime(database)
        runs = [
            start_run(runtime, definition, version, admin, tmp_path, {"seed": index})
            for index in range(4)
        ]

        entries = runtime.claim_due(limit=8)
        assert counts_by_run(database, entries) == {run["id"]: 1 for run in runs}
        for point_id, token in entries:
            assert runtime.process_point(point_id, token) is True

        # Every Run now has four ready branch points. A scheduler tick with
        # room for eight points must give each Run an equal share instead of
        # draining one wide Run first.
        first_share = runtime.claim_due(limit=8, per_run_limit=4)
        assert counts_by_run(database, first_share) == {
            run["id"]: 2 for run in runs
        }
        for point_id, token in first_share:
            assert runtime.process_point(point_id, token) is True

        second_share = runtime.claim_due(limit=8, per_run_limit=4)
        assert counts_by_run(database, second_share) == {
            run["id"]: 2 for run in runs
        }
        for point_id, token in second_share:
            assert runtime.process_point(point_id, token) is True

        # Drain the remaining branch and Merge transitions. Each tick keeps
        # the same per-Run cap, so a wide Run cannot starve the others.
        rounds = 0
        while any(
            runtime.get_run(run["id"])["status"] == "running" for run in runs
        ):
            rounds += 1
            assert rounds < 50, "fairness drain did not converge"
            claimed = runtime.claim_due(limit=8, per_run_limit=2)
            assert claimed
            assert max(counts_by_run(database, claimed).values()) <= 2
            for point_id, token in claimed:
                assert runtime.process_point(point_id, token) is True

        for run in runs:
            finished = runtime.get_run(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert [branch["index"] for branch in result["branches"]] == [
                0, 1, 2, 3,
            ]
            assert database._one(
                """SELECT COUNT(*) AS count FROM vflow_points
                   WHERE run_id = ? AND node_id = 'after'""",
                (run["id"],),
            )["count"] == 1
            assert finished["point_count_total"] == 6
    finally:
        database.close()


def test_restart_releases_expired_lease_and_processes_point_once(tmp_path):
    database, admin, service = make_service(tmp_path, "lease-restart")
    try:
        definition, version = publish(service, admin, "lease-restart", PASS_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(runtime, definition, version, admin, tmp_path, {})
        claimed = runtime.claim_due(limit=1)
        assert len(claimed) == 1
        point_id, stale_token = claimed[0]

        # Simulate a worker process dying while holding the lease: the SQLite
        # transaction is gone but the lease row stays in the database.
        database.close()
        database = Database(tmp_path / "lease-restart.sqlite3", "secret")
        database.initialize()
        restarted = VFlowRuntime(database)

        unexpired = VFlowRecovery(database).run_once()
        assert unexpired["expired_leases"] == 0
        saved = database._one("SELECT * FROM vflow_points WHERE id = ?", (point_id,))
        assert saved["status"] == "claimed"
        assert saved["lease_token"] == stale_token

        expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_points SET lease_until = ? WHERE id = ?",
                (expired_at, point_id),
            )
        recovered = VFlowRecovery(database).run_once()
        assert recovered["expired_leases"] == 1
        saved = database._one("SELECT * FROM vflow_points WHERE id = ?", (point_id,))
        assert saved["status"] == "ready"
        assert saved["lease_token"] is None
        assert saved["lease_owner"] is None

        # The dead worker cannot commit the point after recovery.
        assert restarted.process_point(point_id, stale_token) is False
        reclaimed = restarted.claim_due(limit=1)
        assert len(reclaimed) == 1
        new_point_id, new_token = reclaimed[0]
        assert new_point_id == point_id
        assert new_token != stale_token
        assert restarted.process_point(new_point_id, new_token) is True

        finished = restarted.get_run(run["id"])
        assert finished["status"] == "succeeded", finished
        assert json.loads(finished["final_result_json"]) == {"ok": True}
        assert database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"] == 1
        assert database._one(
            """SELECT COUNT(*) AS count FROM vflow_events
               WHERE run_id = ? AND event_type = 'RUN_SUCCEEDED'""",
            (run["id"],),
        )["count"] == 1
    finally:
        database.close()


def test_pending_outbox_recovers_without_duplicate_task_enqueue(tmp_path):
    database, admin, service = make_service(tmp_path, "outbox")
    try:
        task = database.create_task(
            admin_id=admin["id"],
            title="Echo",
            description="",
            content="Return JSON",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
            published=True,
        )
        definition, version = publish(
            service,
            admin,
            "outbox",
            TASK_SOURCE.replace("<TASK_ID>", task["id"]),
        )
        gateway = IdempotentGateway()
        runtime = VFlowRuntime(database, gateway)
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"payload": "hello"}
        )
        point = database._one(
            "SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],)
        )

        # Crash-window state: the point is waiting and the outbox intent is
        # durable, but the Task link was never written.
        now = utcnow()
        dedupe_key = (
            f"vflow:{run['id']}:{point['id']}:{point['visit_seq']}:{point['attempt']}"
        )
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'waiting_task', next_check_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (now, point["id"]),
            )
            connection.execute(
                """INSERT INTO vflow_outbox
                   (id, dedupe_key, run_id, point_id, kind, payload_json, status,
                    attempts, next_attempt_at, created_at)
                   VALUES ('pending-outbox', ?, ?, ?, 'task_enqueue', '{}',
                           'pending', 0, ?, ?)""",
                (dedupe_key, run["id"], point["id"], now, now),
            )

        recovered = VFlowRecovery(database).run_once()
        assert recovered["failed_runs"] == 0
        assert recovered["recovered_waits"] == 1
        assert database._one(
            "SELECT COUNT(*) AS count FROM vflow_task_links"
        )["count"] == 0
        saved = database._one(
            "SELECT status, next_check_at FROM vflow_points WHERE id = ?",
            (point["id"],),
        )
        assert saved["status"] == "waiting_task"
        assert saved["next_check_at"] is not None

        claimed = runtime.claim_due(limit=1)
        assert len(claimed) == 1
        point_id, token = claimed[0]
        assert runtime.process_point(point_id, token) is True

        assert gateway.calls == 1
        assert len(gateway.task_runs) == 1
        links = database._all("SELECT * FROM vflow_task_links")
        assert len(links) == 1
        outbox = database._all("SELECT * FROM vflow_outbox")
        assert len(outbox) == 1
        assert outbox[0]["dedupe_key"] == dedupe_key
        assert outbox[0]["status"] == "sent"

        force_due(database)
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded", finished
        assert gateway.calls == 1
        assert len(gateway.task_runs) == 1
        assert database._one(
            "SELECT COUNT(*) AS count FROM vflow_task_links"
        )["count"] == 1
        assert database._one(
            "SELECT COUNT(*) AS count FROM vflow_outbox"
        )["count"] == 1
        assert database._one(
            """SELECT COUNT(*) AS count FROM vflow_events
               WHERE run_id = ? AND event_type = 'TASK_ENQUEUED'""",
            (run["id"],),
        )["count"] == 1

        # A stale worker replay after completion must not enqueue again.
        assert runtime.process_point(point_id, token) is False
        assert gateway.calls == 1
        assert len(gateway.task_runs) == 1
    finally:
        database.close()


def test_open_fork_recovery_is_idempotent_for_several_runs(tmp_path):
    database, admin, service = make_service(tmp_path, "fork-load")
    try:
        definition, version = publish(
            service, admin, "fork-load", RECOVERY_FORK_SOURCE
        )
        runtime = VFlowRuntime(database)
        runs = [
            start_run(runtime, definition, version, admin, tmp_path, {"seed": index})
            for index in range(4)
        ]
        forks: dict[str, str] = {}
        for index, run in enumerate(runs):
            point = database._one(
                "SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],)
            )
            forks[run["id"]] = insert_completed_fork(
                database, run, point, f"run-{index}"
            )

        first = VFlowRecovery(database).run_once()
        assert first["recovered_merges"] == len(runs)
        second = VFlowRecovery(database).run_once()
        assert second["recovered_merges"] == 0

        for run in runs:
            fork = database._one(
                "SELECT * FROM vflow_fork_instances WHERE id = ?",
                (forks[run["id"]],),
            )
            assert fork["status"] == "merged"
            assert fork["continuation_point_id"] is not None
            continuations = database._all(
                "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                (run["id"],),
            )
            assert len(continuations) == 1
            assert continuations[0]["id"] == fork["continuation_point_id"]
            assert database._one(
                """SELECT COUNT(*) AS count FROM vflow_events
                   WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
                (run["id"],),
            )["count"] == 1
            assert database._one(
                "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
                (run["id"],),
            )["count"] == 5

        # Recovered continuations are ordinary ready points and finish exactly
        # once; another recovery pass must not create a second continuation.
        for run in runs:
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert [branch["index"] for branch in result["branches"]] == [
                0, 1, 2,
            ]
            assert result["after"] is True
            assert database._one(
                """SELECT COUNT(*) AS count FROM vflow_points
                   WHERE run_id = ? AND node_id = 'after'""",
                (run["id"],),
            )["count"] == 1
            assert database._one(
                """SELECT COUNT(*) AS count FROM vflow_events
                   WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
                (run["id"],),
            )["count"] == 1

        third = VFlowRecovery(database).run_once()
        assert third["recovered_merges"] == 0
    finally:
        database.close()


def test_sqlite_lock_failures_leave_claim_and_process_retry_safe(tmp_path):
    database, admin, service = make_service(tmp_path, "busy")
    blocker = None
    try:
        definition, version = publish(service, admin, "busy", PASS_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(runtime, definition, version, admin, tmp_path, {})

        # Keep the failure fast and deterministic instead of waiting for
        # SQLite's default multi-second busy timeout.
        database._connection.execute("PRAGMA busy_timeout = 25")
        blocker = sqlite3.connect(database.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            runtime.claim_due(limit=1)
        untouched = database._one(
            "SELECT status, lease_token FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )
        assert untouched == {"status": "ready", "lease_token": None}

        blocker.execute("ROLLBACK")
        claimed = runtime.claim_due(limit=1)
        assert len(claimed) == 1
        point_id, token = claimed[0]

        # The claim transaction is short: another writer can take the lock as
        # soon as claim_due returns.
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            runtime.process_point(point_id, token)
        saved = database._one(
            "SELECT status, lease_token FROM vflow_points WHERE id = ?", (point_id,)
        )
        assert saved == {"status": "claimed", "lease_token": token}

        blocker.execute("ROLLBACK")
        assert runtime.process_point(point_id, token) is True
        finished = runtime.get_run(run["id"])
        assert finished["status"] == "succeeded", finished
        assert database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"] == 1
    finally:
        if blocker is not None:
            blocker.close()
        database.close()
