"""Environment-backed VFlow configuration and feature flag."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .limits import VFlowLimits


@dataclass(frozen=True, slots=True)
class VFlowConfig:
    enabled: bool = True
    limits: VFlowLimits = field(default_factory=VFlowLimits)
    runs_per_minute: int = 30
    rate_window_seconds: float = 60.0
    retention_days: int = 30


def _raw(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    return "" if value is None else value.strip()


def _flag(environ: Mapping[str, str], name: str, default: bool) -> bool:
    normalized = _raw(environ, name).lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {normalized!r}")


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = _raw(environ, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = _raw(environ, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, got {raw!r}")
    return value


def load_config(environ: Mapping[str, str] | None = None) -> VFlowConfig:
    """Read and validate VFlow settings, raising ValueError for bad values."""

    source = os.environ if environ is None else environ
    limits = VFlowLimits(
        max_fork_n=_positive_int(source, "VFLOW_MAX_FORK_N", 32),
        max_active_points=_positive_int(source, "VFLOW_MAX_ACTIVE_POINTS", 128),
        max_total_points=_positive_int(source, "VFLOW_MAX_TOTAL_POINTS", 1_000),
        max_transitions=_positive_int(source, "VFLOW_MAX_TRANSITIONS", 10_000),
        max_json_bytes=_positive_int(source, "VFLOW_MAX_JSON_BYTES", 1_048_576),
        max_json_depth=_positive_int(source, "VFLOW_MAX_JSON_DEPTH", 64),
        max_run_seconds=_positive_int(source, "VFLOW_MAX_RUN_SECONDS", 86_400),
    )
    return VFlowConfig(
        enabled=_flag(source, "VFLOW_ENABLED", True),
        limits=limits,
        runs_per_minute=_positive_int(source, "VFLOW_RUNS_PER_MINUTE", 30),
        rate_window_seconds=_positive_float(source, "VFLOW_RUN_WINDOW_SECONDS", 60.0),
        retention_days=_positive_int(source, "VFLOW_RETENTION_DAYS", 30),
    )
