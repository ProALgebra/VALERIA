"""Read-only VFlow anomaly diagnostics for administrators.

The report intentionally contains only stable identifiers and aggregate
counts. It never reads graph IR, run input, task prompts, task output or
outbox payloads, so it can be exposed through the admin API without redaction
gaps.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..database import Database, utcnow
from .health import (
    ACTIVE_POINT_STATUSES,
    ACTIVE_RUN_STATUSES,
    _required_handlers,
    _split_handler,
)
from .registry import DEFAULT_REGISTRY, NodeRegistry

ANOMALY_CODES = (
    "RUN_STUCK",
    "TASK_LINK_MISSING",
    "TASK_ORPHANED",
    "MERGE_READY",
    "OUTBOX_DEAD",
    "HANDLER_MISSING",
)
ITEM_LIMIT = 100


class VFlowAnomalies:
    """Detect operational inconsistencies without changing VFlow state."""

    def __init__(
        self,
        database: Database,
        registry: NodeRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self.database = database
        self.registry = registry

    def scan(self) -> dict[str, Any]:
        generated_at = utcnow()
        counts = dict.fromkeys(ANOMALY_CODES, 0)
        items: list[dict[str, Any]] = []
        try:
            detectors = (
                ("RUN_STUCK", self._run_stuck()),
                ("TASK_LINK_MISSING", self._task_link_missing()),
                ("TASK_ORPHANED", self._task_orphaned()),
                ("MERGE_READY", self._merge_ready()),
                ("OUTBOX_DEAD", self._outbox_dead()),
                ("HANDLER_MISSING", self._handler_missing()),
            )
        except sqlite3.Error:
            return {
                "status": "degraded",
                "generated_at": generated_at,
                "counts": counts,
                "items": items,
            }

        for code, rows in detectors:
            counts[code] = len(rows)
            remaining = ITEM_LIMIT - len(items)
            if remaining <= 0:
                continue
            items.extend(
                {
                    "code": code,
                    "run_id": row.get("run_id"),
                    "point_id": row.get("point_id"),
                    "node_id": row.get("node_id"),
                }
                for row in rows[:remaining]
            )

        return {
            "status": "degraded" if any(counts.values()) else "ok",
            "generated_at": generated_at,
            "counts": counts,
            "items": items,
        }

    def _run_stuck(self) -> list[dict[str, Any]]:
        return self.database._all(
            f"""SELECT r.id AS run_id, NULL AS point_id, NULL AS node_id
                FROM vflow_runs r
                WHERE r.status = 'running'
                  AND NOT EXISTS (
                    SELECT 1 FROM vflow_points p
                    WHERE p.run_id = r.id
                      AND p.status IN ({_placeholders(ACTIVE_POINT_STATUSES)})
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM vflow_fork_instances f
                    WHERE f.run_id = r.id AND f.status = 'open'
                  )
                ORDER BY r.created_at, r.id""",
            ACTIVE_POINT_STATUSES,
        )

    def _task_link_missing(self) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT p.run_id AS run_id, p.id AS point_id, p.node_id AS node_id
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
                 )
               ORDER BY p.created_at, p.id"""
        )

    def _task_orphaned(self) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT l.run_id AS run_id, l.point_id AS point_id,
                      l.node_id AS node_id
               FROM vflow_task_links l
               LEFT JOIN queue_jobs q ON q.id = l.task_run_id
               WHERE q.id IS NULL
               ORDER BY l.queued_at, l.id"""
        )

    def _merge_ready(self) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT f.run_id AS run_id, f.parent_point_id AS point_id,
                      f.merge_node_id AS node_id
               FROM vflow_fork_instances f
               LEFT JOIN vflow_merge_arrivals a ON a.fork_instance_id = f.id
               WHERE f.status = 'open' AND f.continuation_point_id IS NULL
               GROUP BY f.id
               HAVING COUNT(a.id) = f.expected_count
                  AND COUNT(DISTINCT a.branch_index) = f.expected_count
                  AND MIN(a.branch_index) = 0
                  AND MAX(a.branch_index) = f.expected_count - 1
               ORDER BY f.created_at, f.id"""
        )

    def _outbox_dead(self) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT o.run_id AS run_id, o.point_id AS point_id,
                      p.node_id AS node_id
               FROM vflow_outbox o
               LEFT JOIN vflow_points p ON p.id = o.point_id
               WHERE o.status = 'dead'
               ORDER BY o.created_at, o.id"""
        )

    def _handler_missing(self) -> list[dict[str, Any]]:
        rows = self.database._all(
            """SELECT r.id AS run_id, v.required_handlers_json
               FROM vflow_runs r
               LEFT JOIN vflow_versions v ON v.id = r.version_id
               WHERE r.status IN (?, ?)
               ORDER BY r.created_at, r.id""",
            ACTIVE_RUN_STATUSES,
        )
        missing_runs: list[dict[str, Any]] = []
        for row in rows:
            required, invalid_reason = _required_handlers(
                row.get("required_handlers_json")
            )
            if invalid_reason is not None:
                missing_runs.append(
                    {"run_id": row["run_id"], "point_id": None, "node_id": None}
                )
                continue
            if any(
                self.registry.get(*_split_handler(handler)) is None
                for handler in required
            ):
                missing_runs.append(
                    {"run_id": row["run_id"], "point_id": None, "node_id": None}
                )
        return missing_runs


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)
