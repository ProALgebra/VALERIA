from __future__ import annotations

from typing import Any

from ..database import Database, utcnow

ACTIVE_POINT_STATUSES = ("ready", "claimed", "waiting_task", "waiting_merge")


class VFlowMetrics:
    """Read-only operational snapshot built from the VFlow source of truth."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _group(self, table: str, column: str) -> dict[str, int]:
        rows = self.database._all(
            f"SELECT {column} AS key, COUNT(*) AS count FROM {table} GROUP BY {column}"
        )
        return {str(row["key"]): int(row["count"]) for row in rows}

    def snapshot(self) -> dict[str, Any]:
        runs = self._group("vflow_runs", "status")
        points = self._group("vflow_points", "status")
        outbox = self._group("vflow_outbox", "status")
        queue = self._group("queue_jobs", "status")
        open_forks = self.database._one(
            "SELECT COUNT(*) AS count FROM vflow_fork_instances WHERE status = 'open'"
        )
        errors = self.database._all(
            """SELECT error_code, COUNT(*) AS count
               FROM vflow_runs
               WHERE status = 'failed' AND error_code IS NOT NULL
               GROUP BY error_code
               ORDER BY count DESC, error_code
               LIMIT 20"""
        )
        return {
            "generated_at": utcnow(),
            "runs": runs,
            "points": points,
            "outbox": outbox,
            "queue": queue,
            "active_points": sum(points.get(status, 0) for status in ACTIVE_POINT_STATUSES),
            "open_forks": int(open_forks["count"] if open_forks else 0),
            "failed_by_code": [
                {"code": row["error_code"], "count": int(row["count"])} for row in errors
            ],
        }
