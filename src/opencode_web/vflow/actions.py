"""Typed runtime actions planned by VFlow node handlers.

Handlers are pure planners: they inspect an immutable node/point snapshot and
return one of these frozen dataclasses. The runtime is the only component that
persists their consequences. Fields therefore stay minimal and JSON-safe; the
classes deliberately contain no behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Advance:
    """Move the current point along ``port`` carrying ``data``."""

    port: str
    data: Any


@dataclass(frozen=True)
class EnqueueTask:
    """Request a normal VALERIA queue enqueue for a Task point."""

    task_id: str
    input_data: Any
    result_mode: str = "auto"
    queue_timeout_seconds: int | None = None
    execution_timeout_seconds: int | None = None


@dataclass(frozen=True)
class Suspend:
    """Persist a wait without keeping a worker busy."""

    kind: str
    wake_condition: dict[str, Any] | None = None


@dataclass(frozen=True)
class SpawnFork:
    """Create ``count`` branch points which are joined by ``join``."""

    count: int
    join: str
    data: Any


@dataclass(frozen=True)
class ArriveMerge:
    """Register a single branch arrival at a merge point."""

    fork_instance_id: str
    branch_index: int
    data: Any


@dataclass(frozen=True)
class Finish:
    """Consume the current point and finish the run when it is idle."""

    data: Any


@dataclass(frozen=True)
class Fail:
    """Fail the current run with a safe diagnostic."""

    code: str
    message: str


type Action = Advance | EnqueueTask | Suspend | SpawnFork | ArriveMerge | Finish | Fail

__all__ = [
    "Action",
    "Advance",
    "ArriveMerge",
    "EnqueueTask",
    "Fail",
    "Finish",
    "SpawnFork",
    "Suspend",
]
