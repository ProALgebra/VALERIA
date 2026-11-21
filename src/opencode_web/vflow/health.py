"""Read-only VFlow health and deployment compatibility checks.

The snapshot intentionally contains only aggregate counts, run/version
identifiers and registered handler names.  It never reads graph IR, run input,
task prompts or any other user-provided JSON payload.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..database import Database, utcnow
from .registry import DEFAULT_REGISTRY, NodeRegistry

ACTIVE_RUN_STATUSES = ("running", "cancel_requested")
ACTIVE_POINT_STATUSES = ("ready", "claimed", "waiting_task", "waiting_merge")
REQUIRED_TABLES = (
    "vflow_definitions",
    "vflow_versions",
    "vflow_runs",
    "vflow_points",
    "vflow_fork_instances",
    "vflow_merge_arrivals",
    "vflow_task_links",
    "vflow_events",
    "vflow_audit_events",
    "vflow_outbox",
)


class VFlowHealth:
    """Build an operational snapshot without mutating VFlow state."""

    def __init__(
        self,
        database: Database,
        registry: NodeRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self.database = database
        self.registry = registry

    def snapshot(self) -> dict[str, Any]:
        generated_at = utcnow()
        database_check = self._database_check()
        handlers_check = self._handlers_check(
            database_ok=database_check["status"] == "ok"
        )
        counts = self._counts(generated_at)

        if database_check["status"] == "error":
            status = "error"
        elif handlers_check["status"] == "degraded":
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "generated_at": generated_at,
            "checks": {
                "database": database_check,
                "handlers": handlers_check,
            },
            "counts": counts,
        }

    def _database_check(self) -> dict[str, Any]:
        try:
            ping = self.database._one("SELECT 1 AS ok")
        except sqlite3.Error:
            return {
                "status": "error",
                "error": "DATABASE_UNAVAILABLE",
                "missing_tables": [],
                "unreadable_tables": [],
            }
        if not ping or int(ping.get("ok") or 0) != 1:
            return {
                "status": "error",
                "error": "DATABASE_UNAVAILABLE",
                "missing_tables": [],
                "unreadable_tables": [],
            }

        try:
            present = {
                str(row["name"])
                for row in self.database._all(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        except sqlite3.Error:
            return {
                "status": "error",
                "error": "SCHEMA_UNAVAILABLE",
                "missing_tables": [],
                "unreadable_tables": [],
            }

        missing_tables = [table for table in REQUIRED_TABLES if table not in present]
        unreadable_tables: list[str] = []
        for table in REQUIRED_TABLES:
            if table in missing_tables:
                continue
            try:
                # Presence alone is not enough: an old/corrupt deployment may
                # expose a table that fails when the runtime actually queries it.
                self.database._one(f'SELECT 1 AS ok FROM "{table}" LIMIT 1')
            except sqlite3.Error:
                unreadable_tables.append(table)

        if missing_tables or unreadable_tables:
            return {
                "status": "error",
                "error": "SCHEMA_INCOMPATIBLE",
                "missing_tables": missing_tables,
                "unreadable_tables": unreadable_tables,
            }
        return {
            "status": "ok",
            "missing_tables": [],
            "unreadable_tables": [],
        }

    def _handlers_check(self, *, database_ok: bool) -> dict[str, Any]:
        if not database_ok:
            return {
                "status": "skipped",
                "active_runs_checked": 0,
                "incompatible_versions": [],
                "invalid_versions": [],
            }
        try:
            versions = self.database._all(
                """SELECT r.version_id AS version_id,
                          v.required_handlers_json AS required_handlers_json,
                          COUNT(*) AS run_count
                   FROM vflow_runs r
                   LEFT JOIN vflow_versions v ON v.id = r.version_id
                   WHERE r.status IN (?, ?)
                   GROUP BY r.version_id, v.required_handlers_json
                   ORDER BY r.version_id""",
                ACTIVE_RUN_STATUSES,
            )
        except sqlite3.Error:
            return {
                "status": "degraded",
                "error": "HANDLERS_UNAVAILABLE",
                "active_runs_checked": 0,
                "incompatible_versions": [],
                "invalid_versions": [],
            }

        active_runs = 0
        incompatible: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for row in versions:
            run_count = int(row["run_count"] or 0)
            active_runs += run_count
            version_id = str(row["version_id"])
            required, invalid_reason = _required_handlers(row["required_handlers_json"])
            if invalid_reason is not None:
                invalid.append(
                    {
                        "version_id": version_id,
                        "run_count": run_count,
                        "reason": invalid_reason,
                    }
                )
                continue
            missing = [
                handler
                for handler in required
                if self.registry.get(*_split_handler(handler)) is None
            ]
            if missing:
                incompatible.append(
                    {
                        "version_id": version_id,
                        "run_count": run_count,
                        "missing_handlers": missing,
                    }
                )

        if incompatible or invalid:
            return {
                "status": "degraded",
                "active_runs_checked": active_runs,
                "incompatible_versions": incompatible,
                "invalid_versions": invalid,
            }
        return {
            "status": "ok",
            "active_runs_checked": active_runs,
            "incompatible_versions": [],
            "invalid_versions": [],
        }

    def _counts(self, now: str) -> dict[str, int]:
        try:
            row = self.database._one(
                """SELECT
                     (SELECT COUNT(*) FROM vflow_runs
                      WHERE status IN (?, ?))
                       AS active_runs,
                     (SELECT COUNT(*) FROM vflow_points
                      WHERE status IN (?, ?, ?, ?))
                       AS active_points,
                     (SELECT COUNT(*) FROM vflow_fork_instances
                      WHERE status = 'open')
                       AS open_forks,
                     (SELECT COUNT(*) FROM vflow_points
                      WHERE status = 'claimed'
                        AND lease_until IS NOT NULL
                        AND lease_until <= ?)
                       AS expired_leases,
                     (SELECT COUNT(*) FROM vflow_outbox
                      WHERE status = 'pending')
                       AS outbox_pending,
                     (SELECT COUNT(*) FROM vflow_outbox
                      WHERE status = 'processing')
                       AS outbox_processing""",
                (*ACTIVE_RUN_STATUSES, *ACTIVE_POINT_STATUSES, now),
            )
        except sqlite3.Error:
            row = None
        return {
            "active_runs": int((row or {}).get("active_runs") or 0),
            "active_points": int((row or {}).get("active_points") or 0),
            "open_forks": int((row or {}).get("open_forks") or 0),
            "expired_leases": int((row or {}).get("expired_leases") or 0),
            "outbox_pending": int((row or {}).get("outbox_pending") or 0),
            "outbox_processing": int((row or {}).get("outbox_processing") or 0),
        }


def _required_handlers(value: Any) -> tuple[list[str], str | None]:
    if not isinstance(value, str) or not value:
        return [], "required_handlers_missing"
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return [], "required_handlers_invalid"
    if not isinstance(loaded, list):
        return [], "required_handlers_invalid"
    handlers: list[str] = []
    for item in loaded:
        if not isinstance(item, str):
            return [], "required_handlers_invalid"
        type_id, separator, version = item.rpartition("@")
        if not separator or not type_id or not version:
            return [], "required_handlers_invalid"
        handlers.append(item)
    return sorted(set(handlers)), None


def _split_handler(value: str) -> tuple[str, str]:
    type_id, _separator, version = value.rpartition("@")
    return type_id, version
