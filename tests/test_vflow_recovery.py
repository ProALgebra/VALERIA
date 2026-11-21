from __future__ import annotations

import json

from opencode_web.database import Database, utcnow
from opencode_web.vflow.recovery import VFlowRecovery
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "recovery" {
  version: 1;
  input: json;
  step pass = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> pass -> end
}
"""

FORK_SOURCE = """\
workflow "recovery-fork" {
  version: 1;
  input: json;
  step split = fork(count: 3, join: combined);
  step combined = merge();
  step after = transform(operations: [
    {op: "set", path: "$.after", value: true}
  ]);
  start -> split;
  split[0] -> combined;
  split[1] -> combined;
  split[2] -> combined;
  combined -> after -> end
}
"""

FORK_END_SOURCE = """\
workflow "recovery-fork-end" {
  version: 1;
  input: json;
  step split = fork(count: 2, join: combined);
  step combined = merge();
  start -> split;
  split[0] -> combined;
  split[1] -> combined;
  combined -> end
}
"""


def make_run(tmp_path, source=SOURCE, slug="recovery"):
    database = Database(tmp_path / "recovery.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", None)
    service = VFlowService(database)
    definition = service.create_definition(admin["id"], slug=slug, title="Recovery")
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    runtime = VFlowRuntime(database)
    run = runtime.create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=admin["id"],
        workspace_id=str(tmp_path / "workspace"),
        input_data={},
    )
    point = database._one("SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],))
    return database, run, point


def update_point(database, point_id, **values):
    columns = ", ".join(f"{key} = ?" for key in values)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE vflow_points SET {columns} WHERE id = ?",
            (*values.values(), point_id),
        )


def insert_completed_fork(
    database,
    run,
    point,
    *,
    expected_count=3,
    arrivals=None,
    parent_stack=None,
    source_data=None,
    fork_node_id="split",
    merge_node_id="combined",
):
    now = utcnow()
    fork_id = "fork-instance-1"
    parent_stack = parent_stack or []
    source_data = source_data if source_data is not None else {"seed": 1}
    if arrivals is None:
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                fork_id,
                run["id"],
                fork_node_id,
                merge_node_id,
                point["id"],
                point["visit_seq"],
                expected_count,
                json.dumps(source_data),
                json.dumps(parent_stack),
                now,
            ),
        )
        for branch_index, data in enumerate(arrivals):
            arrival_point_id = f"arrival-point-{branch_index}"
            frame = {
                "fork_instance_id": fork_id,
                "fork_node_id": fork_node_id,
                "merge_node_id": merge_node_id,
                "branch_index": branch_index,
                "expected_count": expected_count,
            }
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, ?, 'waiting_merge', ?, ?, ?, ?, 1, ?, ?)""",
                (
                    arrival_point_id,
                    run["id"],
                    merge_node_id,
                    json.dumps(data),
                    json.dumps([*parent_stack, frame]),
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
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"arrival-{branch_index}",
                    fork_id,
                    merge_node_id,
                    branch_index,
                    arrival_point_id,
                    json.dumps(data),
                    now,
                ),
            )
    return fork_id


def test_expired_lease_is_released(tmp_path):
    database, run, point = make_run(tmp_path)
    try:
        update_point(
            database,
            point["id"],
            status="claimed",
            lease_owner="test",
            lease_token="token",
            lease_until="2000-01-01T00:00:00+00:00",
        )
        result = VFlowRecovery(database).run_once()
        recovered = database._one("SELECT * FROM vflow_points WHERE id = ?", (point["id"],))
        assert result["expired_leases"] == 1
        assert recovered["status"] == "ready"
        assert recovered["lease_owner"] is None
        assert recovered["lease_token"] is None
    finally:
        database.close()


def test_missing_task_link_fails_run_without_duplicate(tmp_path):
    database, run, point = make_run(tmp_path)
    try:
        update_point(database, point["id"], status="waiting_task")
        result = VFlowRecovery(database).run_once()
        failed = database._one("SELECT * FROM vflow_runs WHERE id = ?", (run["id"],))
        assert result["failed_runs"] == 1
        assert failed["status"] == "failed"
        assert failed["error_code"] == "RECOVERY_TASK_LINK_MISSING"
        assert database._all("SELECT * FROM vflow_task_links WHERE run_id = ?", (run["id"],)) == []
    finally:
        database.close()


def test_waiting_task_with_link_is_scheduled(tmp_path):
    database, run, point = make_run(tmp_path)
    try:
        update_point(database, point["id"], status="waiting_task", next_check_at=None)
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO vflow_task_links
                   (id, run_id, point_id, node_id, visit_seq, attempt, task_id,
                    task_run_id, enqueue_key, status, queued_at)
                   VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, 'queued', ?)""",
                (
                    "link-1",
                    run["id"],
                    point["id"],
                    point["node_id"],
                    "task-1",
                    "job-1",
                    "enqueue-1",
                    utcnow(),
                ),
            )
        result = VFlowRecovery(database).run_once()
        recovered = database._one("SELECT * FROM vflow_points WHERE id = ?", (point["id"],))
        assert result["recovered_waits"] == 1
        assert recovered["status"] == "waiting_task"
        assert recovered["next_check_at"] is not None
    finally:
        database.close()


def test_stuck_run_fails(tmp_path):
    database, run, point = make_run(tmp_path)
    try:
        update_point(database, point["id"], status="consumed")
        result = VFlowRecovery(database).run_once()
        failed = database._one("SELECT * FROM vflow_runs WHERE id = ?", (run["id"],))
        assert result["failed_runs"] == 1
        assert failed["status"] == "failed"
        assert failed["error_code"] == "RUN_STUCK"
    finally:
        database.close()


def test_pending_outbox_is_scheduled_not_failed(tmp_path):
    database, run, point = make_run(tmp_path)
    try:
        now = utcnow()
        update_point(database, point["id"], status="waiting_task")
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO vflow_outbox
                   (id, dedupe_key, run_id, point_id, kind, payload_json,
                    status, attempts, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, 'task_enqueue', '{}', 'pending', 0, ?, ?)""",
                ("outbox-1", "dedupe-1", run["id"], point["id"], now, now),
            )

        result = VFlowRecovery(database).run_once()
        assert result["failed_runs"] == 0
        assert result["recovered_waits"] == 1
        saved_run = database._one("SELECT status FROM vflow_runs WHERE id = ?", (run["id"],))
        saved_point = database._one(
            "SELECT status, next_check_at FROM vflow_points WHERE id = ?",
            (point["id"],),
        )
        assert saved_run["status"] == "running"
        assert saved_point["status"] == "waiting_task"
        assert saved_point["next_check_at"] is not None
    finally:
        database.close()


def test_completed_fork_merge_is_recovered_once(tmp_path):
    database, run, point = make_run(tmp_path, source=FORK_SOURCE, slug="recovery-fork")
    try:
        fork_id = insert_completed_fork(
            database,
            run,
            point,
            arrivals=[{"branch": 0}, {"branch": 1}, {"branch": 2}],
        )
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_points
                   SET lease_owner = 'dead-worker', lease_token = 'dead-token',
                       lease_until = ?
                   WHERE id IN (
                     SELECT point_id FROM vflow_merge_arrivals
                     WHERE fork_instance_id = ?
                   )""",
                (utcnow(), fork_id),
            )

        result = VFlowRecovery(database).run_once()
        assert result["recovered_merges"] == 1

        fork = database._one("SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_id,))
        assert fork["status"] == "merged"
        assert fork["merged_at"] is not None
        assert fork["continuation_point_id"] is not None

        continuations = database._all(
            "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
            (run["id"],),
        )
        assert len(continuations) == 1
        continuation = continuations[0]
        assert continuation["id"] == fork["continuation_point_id"]
        assert continuation["status"] == "ready"
        assert json.loads(continuation["data_json"]) == {
            "source": {"seed": 1},
            "branches": [
                {"index": 0, "data": {"branch": 0}},
                {"index": 1, "data": {"branch": 1}},
                {"index": 2, "data": {"branch": 2}},
            ],
        }

        arrivals = database._all(
            """SELECT p.* FROM vflow_points p
               JOIN vflow_merge_arrivals a ON a.point_id = p.id
               WHERE a.fork_instance_id = ?
               ORDER BY a.branch_index""",
            (fork_id,),
        )
        assert [row["status"] for row in arrivals] == ["consumed", "consumed", "consumed"]
        for row in arrivals:
            assert row["lease_owner"] is None
            assert row["lease_token"] is None
            assert row["lease_until"] is None

        events = database._all(
            """SELECT * FROM vflow_events
               WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
            (run["id"],),
        )
        assert len(events) == 1
        assert json.loads(events[0]["payload_json"]) == {
            "fork_instance_id": fork_id,
            "count": 3,
        }

        points_before = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        second = VFlowRecovery(database).run_once()
        points_after = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        saved_fork = database._one("SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_id,))
        assert second["recovered_merges"] == 0
        assert points_after == points_before
        assert saved_fork["continuation_point_id"] == continuation["id"]
        assert (
            len(
                database._all(
                    "SELECT id FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                    (run["id"],),
                )
            )
            == 1
        )
        assert (
            len(
                database._all(
                    """SELECT id FROM vflow_events
                       WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
                    (run["id"],),
                )
            )
            == 1
        )
    finally:
        database.close()


def test_recovery_preserves_branch_data_and_parent_fork_stack(tmp_path):
    database, run, point = make_run(tmp_path, source=FORK_SOURCE, slug="recovery-fork")
    try:
        parent_stack = [
            {
                "fork_instance_id": "outer-fork",
                "fork_node_id": "outer-split",
                "merge_node_id": "outer-merge",
                "branch_index": 1,
                "expected_count": 2,
            }
        ]
        arrivals = [
            {"side": "left", "nested": {"values": [1, 2, 3]}, "ok": True},
            {"side": "right", "nested": {"values": [4, 5]}, "ok": False},
            {"side": "extra", "text": "кириллица"},
        ]
        insert_completed_fork(
            database,
            run,
            point,
            arrivals=arrivals,
            parent_stack=parent_stack,
        )

        VFlowRecovery(database).run_once()

        continuation = database._one(
            "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
            (run["id"],),
        )
        assert continuation is not None
        data = json.loads(continuation["data_json"])
        assert data["source"] == {"seed": 1}
        assert data["branches"] == [
            {"index": index, "data": arrival} for index, arrival in enumerate(arrivals)
        ]
        assert json.loads(continuation["fork_stack_json"]) == parent_stack
    finally:
        database.close()


def test_incomplete_fork_waits_for_all_arrivals(tmp_path):
    database, run, point = make_run(tmp_path, source=FORK_SOURCE, slug="recovery-fork")
    try:
        fork_id = insert_completed_fork(
            database,
            run,
            point,
            expected_count=3,
            arrivals=[{"branch": 0}, {"branch": 1}],
        )

        first = VFlowRecovery(database).run_once()
        fork = database._one("SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_id,))
        assert first["recovered_merges"] == 0
        assert fork["status"] == "open"
        assert fork["continuation_point_id"] is None
        assert (
            database._all(
                "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                (run["id"],),
            )
            == []
        )

        now = utcnow()
        frame = {
            "fork_instance_id": fork_id,
            "fork_node_id": "split",
            "merge_node_id": "combined",
            "branch_index": 2,
            "expected_count": 3,
        }
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES ('arrival-point-2', ?, 'combined', 'waiting_merge',
                           ?, ?, ?, 4, 1, ?, ?)""",
                (
                    run["id"],
                    json.dumps({"branch": 2}),
                    json.dumps([frame]),
                    point["id"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO vflow_merge_arrivals
                   (id, fork_instance_id, merge_node_id, branch_index, point_id,
                    data_json, arrived_at)
                   VALUES ('arrival-2', ?, 'combined', 2, 'arrival-point-2', ?, ?)""",
                (fork_id, json.dumps({"branch": 2}), now),
            )

        second = VFlowRecovery(database).run_once()
        continuation = database._one(
            "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
            (run["id"],),
        )
        assert second["recovered_merges"] == 1
        assert continuation is not None
        assert json.loads(continuation["data_json"])["branches"] == [
            {"index": 0, "data": {"branch": 0}},
            {"index": 1, "data": {"branch": 1}},
            {"index": 2, "data": {"branch": 2}},
        ]
    finally:
        database.close()


def test_open_fork_on_failed_run_is_cancelled_without_continuation(tmp_path):
    database, run, point = make_run(tmp_path, source=FORK_SOURCE, slug="recovery-fork")
    try:
        fork_id = insert_completed_fork(database, run, point)
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_runs
                   SET status = 'failed', error_code = 'TEST_FAILURE', finished_at = ?
                   WHERE id = ?""",
                (utcnow(), run["id"]),
            )

        points_before = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        result = VFlowRecovery(database).run_once()

        fork = database._one("SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_id,))
        points_after = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        assert result["cancelled_forks"] == 1
        assert result["recovered_merges"] == 0
        assert fork["status"] == "cancelled"
        assert fork["continuation_point_id"] is None
        assert points_after == points_before
        assert (
            database._all(
                "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                (run["id"],),
            )
            == []
        )
        assert (
            database._all(
                """SELECT * FROM vflow_events
                   WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
                (run["id"],),
            )
            == []
        )
    finally:
        database.close()


def test_recovery_finishes_run_when_merge_targets_end(tmp_path):
    database, run, point = make_run(tmp_path, source=FORK_END_SOURCE, slug="recovery-end")
    try:
        fork_id = insert_completed_fork(
            database,
            run,
            point,
            expected_count=2,
            arrivals=[{"side": "left"}, {"side": "right"}],
        )

        result = VFlowRecovery(database).run_once()

        saved_run = database._one("SELECT * FROM vflow_runs WHERE id = ?", (run["id"],))
        fork = database._one("SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_id,))
        assert result["recovered_merges"] == 1
        assert saved_run["status"] == "succeeded"
        assert saved_run["finished_at"] is not None
        assert json.loads(saved_run["final_result_json"]) == {
            "source": {"seed": 1},
            "branches": [
                {"index": 0, "data": {"side": "left"}},
                {"index": 1, "data": {"side": "right"}},
            ],
        }
        assert fork["status"] == "merged"
        assert fork["continuation_point_id"] is None
        assert (
            database._all(
                "SELECT * FROM vflow_points WHERE run_id = ? AND node_id = 'after'",
                (run["id"],),
            )
            == []
        )
        event_types = [
            row["event_type"]
            for row in database._all(
                """SELECT event_type FROM vflow_events
                   WHERE run_id = ? AND event_type IN
                     ('MERGE_RECOVERED', 'RUN_SUCCEEDED')""",
                (run["id"],),
            )
        ]
        assert sorted(event_types) == ["MERGE_RECOVERED", "RUN_SUCCEEDED"]
    finally:
        database.close()
