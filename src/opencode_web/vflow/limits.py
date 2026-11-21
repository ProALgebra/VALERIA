from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VFlowLimits:
    max_fork_n: int = 32
    max_active_points: int = 128
    max_total_points: int = 1_000
    max_transitions: int = 10_000
    max_json_bytes: int = 1_048_576
    max_json_depth: int = 64
    max_run_seconds: int = 86_400


DEFAULT_LIMITS = VFlowLimits()
LIMIT_FIELDS = (
    "max_fork_n",
    "max_active_points",
    "max_total_points",
    "max_transitions",
    "max_json_bytes",
    "max_json_depth",
    "max_run_seconds",
)


def limits_to_dict(limits: VFlowLimits) -> dict[str, int]:
    return {name: int(getattr(limits, name)) for name in LIMIT_FIELDS}


def limits_from_json(value: str | None, fallback: VFlowLimits = DEFAULT_LIMITS) -> VFlowLimits:
    """Restore pinned limits, falling back to global limits for legacy runs."""
    if not value:
        return fallback
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    if not isinstance(loaded, dict) or set(loaded) != set(LIMIT_FIELDS):
        return fallback
    values: dict[str, int] = {}
    for name in LIMIT_FIELDS:
        item = loaded[name]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            return fallback
        values[name] = item
    return VFlowLimits(**values)


class VFlowLimitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((json_depth(item) for item in value), default=0)
    return 0


def check_json(
    value: Any,
    limits: VFlowLimits = DEFAULT_LIMITS,
    *,
    label: str = "JSON value",
) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise VFlowLimitError(
            "LIMIT_JSON_INVALID", f"{label} is not valid JSON"
        ) from exc
    size = len(encoded.encode("utf-8"))
    if size > limits.max_json_bytes:
        raise VFlowLimitError(
            "LIMIT_JSON_BYTES",
            f"{label} exceeds {limits.max_json_bytes} bytes",
        )
    depth = json_depth(value)
    if depth > limits.max_json_depth:
        raise VFlowLimitError(
            "LIMIT_JSON_DEPTH",
            f"{label} exceeds depth {limits.max_json_depth}",
        )


def check_fork_count(count: int, limits: VFlowLimits = DEFAULT_LIMITS) -> None:
    if count < 2:
        raise VFlowLimitError("LIMIT_FORK_COUNT", "Fork count must be at least 2")
    if count > limits.max_fork_n:
        raise VFlowLimitError(
            "LIMIT_FORK_COUNT",
            f"Fork count exceeds {limits.max_fork_n}",
        )


def check_point_budget(
    active_points: int,
    total_points: int,
    additions: int,
    limits: VFlowLimits = DEFAULT_LIMITS,
) -> None:
    if active_points + additions > limits.max_active_points:
        raise VFlowLimitError(
            "LIMIT_ACTIVE_POINTS",
            f"Active execution points exceed {limits.max_active_points}",
        )
    if total_points + additions > limits.max_total_points:
        raise VFlowLimitError(
            "LIMIT_TOTAL_POINTS",
            f"Total execution points exceed {limits.max_total_points}",
        )
