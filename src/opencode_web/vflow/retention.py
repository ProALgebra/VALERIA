"""Retention policy and background job for finished VFlow runs.

Definitions and published versions are never removed here. Only terminal runs
(``succeeded``, ``failed``, ``cancelled``) older than the configured retention
window are deleted together with their runtime data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..database import Database

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")
ACTIVE_RUN_STATUSES = ("created", "running", "cancel_requested")
COUNT_KEYS = ("runs", "points", "forks", "arrivals", "task_links", "events", "outbox")

_ELIGIBLE_RUNS = """
    SELECT id FROM vflow_runs
    WHERE status IN ('succeeded', 'failed', 'cancelled')
      AND COALESCE(finished_at, created_at) <= ?
"""


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        current = datetime.fromisoformat(value)
    else:
        raise TypeError("now must be a datetime, an ISO-8601 string, or None")
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


class VFlowRetention:
    """Delete terminal VFlow runs that fell out of the retention window.

    ``run_once`` executes in a single ``BEGIN IMMEDIATE`` transaction: the
    counts it returns are exactly the rows it removed (or would remove in
    dry-run mode). Active runs and definitions/versions are always preserved.
    """

    def __init__(self, database: Database, retention_days: int) -> None:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise TypeError("retention_days must be a positive integer")
        if retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        self.database = database
        self.retention_days = retention_days

    def cutoff(self, now: datetime | str | None = None) -> str:
        return (_as_utc(now) - timedelta(days=self.retention_days)).isoformat()

    def run_once(
        self,
        now: datetime | str | None = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        cutoff = self.cutoff(now)
        with self.database.transaction() as connection:
            counts = self._counts(connection, cutoff)
            if not dry_run:
                self._delete(connection, cutoff)
        return counts

    def stats(self, now: datetime | str | None = None) -> dict[str, Any]:
        cutoff = self.cutoff(now)
        with self.database.transaction() as connection:
            eligible = self._counts(connection, cutoff)
            total_runs = self._count(connection, "SELECT COUNT(*) FROM vflow_runs")
            active_runs = self._count(
                connection,
                """SELECT COUNT(*) FROM vflow_runs
                   WHERE status IN ('created', 'running', 'cancel_requested')""",
            )
        return {
            "retention_days": self.retention_days,
            "cutoff": cutoff,
            "eligible": eligible,
            "active_runs": active_runs,
            "total_runs": total_runs,
        }

    @staticmethod
    def _count(
        connection: Any,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> int:
        row = connection.execute(query, params).fetchone()
        return int(row[0]) if row is not None else 0

    def _counts(self, connection: Any, cutoff: str) -> dict[str, int]:
        return {
            "runs": self._count(
                connection,
                "SELECT COUNT(*) FROM vflow_runs "
                "WHERE status IN ('succeeded', 'failed', 'cancelled') "
                "AND COALESCE(finished_at, created_at) <= ?",
                (cutoff,),
            ),
            "points": self._count(
                connection,
                f"SELECT COUNT(*) FROM vflow_points WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            ),
            "forks": self._count(
                connection,
                f"SELECT COUNT(*) FROM vflow_fork_instances WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            ),
            "arrivals": self._count(
                connection,
                """SELECT COUNT(*) FROM vflow_merge_arrivals
                   WHERE fork_instance_id IN (
                     SELECT id FROM vflow_fork_instances
                     WHERE run_id IN (SELECT id FROM vflow_runs
                                      WHERE status IN ('succeeded', 'failed', 'cancelled')
                                        AND COALESCE(finished_at, created_at) <= ?)
                   )""",
                (cutoff,),
            ),
            "task_links": self._count(
                connection,
                f"SELECT COUNT(*) FROM vflow_task_links WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            ),
            "events": self._count(
                connection,
                f"SELECT COUNT(*) FROM vflow_events WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            ),
            "outbox": self._count(
                connection,
                f"SELECT COUNT(*) FROM vflow_outbox WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            ),
        }

    @staticmethod
    def _delete(connection: Any, cutoff: str) -> None:
        # Child rows first so no foreign key is ever temporarily violated.
        # ``vflow_merge_arrivals`` references both a fork instance and a point
        # and has no cascade from points, so it must go before both.
        connection.execute(
            """DELETE FROM vflow_merge_arrivals
               WHERE fork_instance_id IN (
                 SELECT id FROM vflow_fork_instances
                 WHERE run_id IN (SELECT id FROM vflow_runs
                                  WHERE status IN ('succeeded', 'failed', 'cancelled')
                                    AND COALESCE(finished_at, created_at) <= ?)
               )""",
            (cutoff,),
        )
        for table in ("vflow_task_links", "vflow_outbox", "vflow_events"):
            connection.execute(
                f"DELETE FROM {table} WHERE run_id IN ({_ELIGIBLE_RUNS})",
                (cutoff,),
            )
        connection.execute(
            f"DELETE FROM vflow_fork_instances WHERE run_id IN ({_ELIGIBLE_RUNS})",
            (cutoff,),
        )
        connection.execute(
            f"DELETE FROM vflow_points WHERE run_id IN ({_ELIGIBLE_RUNS})",
            (cutoff,),
        )
        # Re-check status and cutoff defensively; active runs are never removed.
        connection.execute(
            """DELETE FROM vflow_runs
               WHERE status IN ('succeeded', 'failed', 'cancelled')
                 AND COALESCE(finished_at, created_at) <= ?""",
            (cutoff,),
        )


class VFlowRetentionScheduler:
    """Lightweight periodic loop that runs retention off the event loop thread."""

    def __init__(
        self,
        retention: VFlowRetention,
        *,
        interval_seconds: float = 86_400.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.retention = retention
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        # The loop performs its first pass immediately on startup.
        self._task = asyncio.create_task(self._run(), name="vflow-retention")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run_once(self, *, dry_run: bool = False) -> dict[str, int]:
        return await asyncio.to_thread(self.retention.run_once, dry_run=dry_run)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                counts = await self.run_once()
                if any(counts.values()):
                    logger.info("vflow retention removed %s", counts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("vflow retention iteration failed")
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
