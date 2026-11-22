from __future__ import annotations

import json
import uuid
from typing import Any

from ..database import Database, utcnow
from .models import Graph
from .redaction import redact_metadata

ACTIVE_POINT_STATUSES = (
    "ready",
    "claimed",
    "waiting_task",
    "processing_output",
    "waiting_merge",
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class VFlowRecovery:
    """Conservative startup recovery for VFlow runs.

    Recovery never re-enqueues a task when its task link is missing: failing the
    run is safer than creating a duplicate VALERIA task.

    Completed Fork/Merge groups are released at most once. The fork instance is
    claimed with a compare-and-set update before a continuation point is created,
    so concurrent recovery passes cannot duplicate downstream work.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def run_once(self) -> dict[str, int]:
        now = utcnow()
        expired = 0
        recovered_waits = 0
        recovered_merges = 0
        cancelled_forks = 0
        failed_runs = 0
        with self.database.transaction() as connection:
            expired = connection.execute(
                """UPDATE vflow_points
                   SET status = 'ready', lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE status = 'claimed'
                     AND lease_until IS NOT NULL
                     AND lease_until <= ?""",
                (now, now),
            ).rowcount

            missing = connection.execute(
                """SELECT p.id, p.run_id, p.node_id
                   FROM vflow_points p
                   WHERE p.status = 'waiting_task'
                     AND NOT EXISTS (
                       SELECT 1 FROM vflow_task_links l WHERE l.point_id = p.id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM vflow_outbox o
                       WHERE o.point_id = p.id
                         AND o.kind = 'task_enqueue'
                         AND o.status IN ('pending', 'processing')
                     )"""
            ).fetchall()
            for point in missing:
                self._fail_run(
                    connection,
                    point["run_id"],
                    "RECOVERY_TASK_LINK_MISSING",
                    "Task execution link is missing",
                    now,
                )
                failed_runs += 1

            recovered_waits = connection.execute(
                """UPDATE vflow_points
                   SET next_check_at = ?,
                       task_run_id = COALESCE(
                           task_run_id,
                           (SELECT l.task_run_id FROM vflow_task_links l
                            WHERE l.point_id = vflow_points.id LIMIT 1)
                       ),
                       updated_at = ?
                   WHERE status = 'waiting_task'
                     AND (
                       EXISTS (
                         SELECT 1 FROM vflow_task_links l
                         WHERE l.point_id = vflow_points.id
                       )
                       OR EXISTS (
                         SELECT 1 FROM vflow_outbox o
                         WHERE o.point_id = vflow_points.id
                           AND o.kind = 'task_enqueue'
                           AND o.status IN ('pending', 'processing')
                       )
                     )""",
                (now, now),
            ).rowcount
            recovered_waits += connection.execute(
                """UPDATE vflow_points
                   SET next_check_at = ?, updated_at = ?
                   WHERE status = 'processing_output'
                     AND json_processor_job_id IS NOT NULL""",
                (now, now),
            ).rowcount

            recovered_merges = self._recover_completed_merges(connection, now)

            cancelled_forks = connection.execute(
                """UPDATE vflow_fork_instances
                   SET status = 'cancelled'
                   WHERE status = 'open'
                     AND run_id IN (
                       SELECT id FROM vflow_runs
                       WHERE status IN ('succeeded', 'failed', 'cancelled')
                     )"""
            ).rowcount

            stuck = connection.execute(
                """SELECT r.id FROM vflow_runs r
                   WHERE r.status = 'running'
                     AND NOT EXISTS (
                       SELECT 1 FROM vflow_points p
                       WHERE p.run_id = r.id
                         AND p.status IN ('ready', 'claimed', 'waiting_task',
                                          'processing_output', 'waiting_merge')
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM vflow_fork_instances f
                       WHERE f.run_id = r.id AND f.status = 'open'
                     )"""
            ).fetchall()
            for run in stuck:
                self._fail_run(
                    connection,
                    run["id"],
                    "RUN_STUCK",
                    "Run has no active points",
                    now,
                )
                failed_runs += 1

        return {
            "expired_leases": expired,
            "recovered_waits": recovered_waits,
            "recovered_merges": recovered_merges,
            "cancelled_forks": cancelled_forks,
            "failed_runs": failed_runs,
        }

    def _recover_completed_merges(self, connection: Any, now: str) -> int:
        instances = connection.execute(
            """SELECT f.*, v.graph_ir_json
               FROM vflow_fork_instances f
               JOIN vflow_runs r ON r.id = f.run_id
               JOIN vflow_versions v ON v.id = r.version_id
               WHERE f.status = 'open' AND r.status = 'running'
               ORDER BY f.created_at, f.id"""
        ).fetchall()
        recovered = 0
        for instance in instances:
            arrivals = connection.execute(
                """SELECT * FROM vflow_merge_arrivals
                   WHERE fork_instance_id = ?
                   ORDER BY branch_index""",
                (instance["id"],),
            ).fetchall()
            expected = int(instance["expected_count"])
            if len(arrivals) != expected:
                continue
            indexes = [int(row["branch_index"]) for row in arrivals]
            if indexes != list(range(expected)):
                continue

            try:
                graph = Graph.from_dict(_load(instance["graph_ir_json"], {}))
            except (AttributeError, KeyError, TypeError, ValueError):
                self._fail_run(
                    connection,
                    instance["run_id"],
                    "RECOVERY_MERGE_GRAPH_INVALID",
                    "Fork/merge graph is unavailable",
                    now,
                )
                continue
            edge = next(
                (
                    item
                    for item in graph.edges
                    if item.source == instance["merge_node_id"] and item.source_port == "out"
                ),
                None,
            )
            if edge is None:
                self._fail_run(
                    connection,
                    instance["run_id"],
                    "RECOVERY_MERGE_EDGE_MISSING",
                    "Merge has no outgoing edge",
                    now,
                )
                continue

            output = {
                "source": _load(instance["source_data_json"], None),
                "branches": [
                    {
                        "index": int(row["branch_index"]),
                        "data": _load(row["data_json"], None),
                    }
                    for row in arrivals
                ],
            }
            continuation_id: str | None = None
            next_visit: int | None = None
            if edge.target != "end":
                continuation_id = str(uuid.uuid4())
                next_visit = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(visit_seq), 0) + 1
                           FROM vflow_points WHERE run_id = ?""",
                        (instance["run_id"],),
                    ).fetchone()[0]
                )

            claimed = connection.execute(
                """UPDATE vflow_fork_instances
                   SET status = 'merged', continuation_point_id = ?, merged_at = ?
                   WHERE id = ? AND status = 'open'""",
                (continuation_id, now, instance["id"]),
            )
            if not claimed.rowcount:
                continue

            connection.execute(
                """UPDATE vflow_points
                   SET status = 'consumed', lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE id IN (
                     SELECT point_id FROM vflow_merge_arrivals
                     WHERE fork_instance_id = ?
                   )""",
                (now, instance["id"]),
            )

            if continuation_id is not None and next_visit is not None:
                connection.execute(
                    """INSERT INTO vflow_points
                       (id, run_id, node_id, status, data_json, fork_stack_json,
                        parent_point_id, visit_seq, attempt, created_at, updated_at)
                       VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        continuation_id,
                        instance["run_id"],
                        edge.target,
                        _dump(output),
                        instance["parent_fork_stack_json"],
                        arrivals[-1]["point_id"],
                        next_visit,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE vflow_runs
                       SET point_count_total = point_count_total + 1,
                           transition_count = transition_count + 1
                       WHERE id = ?""",
                    (instance["run_id"],),
                )
            else:
                self._finish_recovered_run(
                    connection,
                    instance["run_id"],
                    output,
                    instance["merge_node_id"],
                    now,
                )

            self._event(
                connection,
                instance["run_id"],
                arrivals[-1]["point_id"],
                instance["merge_node_id"],
                "MERGE_RECOVERED",
                {"fork_instance_id": instance["id"], "count": expected},
                now,
            )
            recovered += 1
        return recovered

    def _finish_recovered_run(
        self,
        connection: Any,
        run_id: str,
        output: Any,
        node_id: str,
        now: str,
    ) -> bool:
        active = connection.execute(
            """SELECT COUNT(*) FROM vflow_points
               WHERE run_id = ? AND status IN
                 ('ready', 'claimed', 'waiting_task', 'processing_output',
                  'waiting_merge')""",
            (run_id,),
        ).fetchone()[0]
        open_forks = connection.execute(
            """SELECT COUNT(*) FROM vflow_fork_instances
               WHERE run_id = ? AND status = 'open'""",
            (run_id,),
        ).fetchone()[0]
        if active or open_forks:
            return False
        updated = connection.execute(
            """UPDATE vflow_runs
               SET status = 'succeeded', final_result_json = ?,
                   public_result_json = NULL, finished_at = ?,
                   transition_count = transition_count + 1
               WHERE id = ? AND status = 'running'""",
            (_dump(output), now, run_id),
        )
        if not updated.rowcount:
            return False
        self._event(
            connection,
            run_id,
            None,
            node_id,
            "RUN_SUCCEEDED",
            {"recovery": True},
            now,
        )
        return True

    def _fail_run(
        self,
        connection: Any,
        run_id: str,
        code: str,
        message: str,
        now: str,
    ) -> None:
        updated = connection.execute(
            """UPDATE vflow_runs
               SET status = 'failed', error_code = ?, safe_error_message = ?,
                   finished_at = ?
               WHERE id = ? AND status IN ('running', 'cancel_requested')""",
            (code, message, now, run_id),
        )
        if not updated.rowcount:
            return
        connection.execute(
            """UPDATE vflow_points
               SET status = 'cancelled', updated_at = ?
               WHERE run_id = ?
                 AND status IN ('ready', 'claimed', 'waiting_task',
                                'processing_output', 'waiting_merge')""",
            (now, run_id),
        )
        connection.execute(
            """UPDATE vflow_fork_instances
               SET status = 'cancelled'
               WHERE run_id = ? AND status = 'open'""",
            (run_id,),
        )
        self._event(
            connection,
            run_id,
            None,
            None,
            "RUN_FAILED",
            {"recovery": True, "code": code},
            now,
        )

    @staticmethod
    def _event(
        connection: Any,
        run_id: str,
        point_id: str | None,
        node_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO vflow_events
               (run_id, point_id, node_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, point_id, node_id, event_type, _dump(redact_metadata(payload)), now),
        )
