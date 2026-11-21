"""Validated per-pipeline policy stored in ``vflow_definitions.settings_json``.

The policy may tighten global runtime limits but never relax them.  Existing
definitions created before this shape was introduced are treated leniently at
read time so a malformed legacy value cannot make a pipeline unusable.
"""

from __future__ import annotations

import json
from typing import Any

from .limits import DEFAULT_LIMITS, LIMIT_FIELDS, VFlowLimits

POLICY_FIELDS = frozenset({"limits", "cancel_own"})


class PolicyError(ValueError):
    pass


def _unknown(value: dict[str, Any], allowed: frozenset[str] | set[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise PolicyError(f"Unknown {label}: {', '.join(unknown)}")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolicyError(f"{name} must be a positive integer")
    return value


def validate_policy(
    settings: Any,
    caps: VFlowLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Validate and normalize a policy, keeping only explicitly supplied keys."""
    if not isinstance(settings, dict):
        raise PolicyError("settings must be a JSON object")
    _unknown(settings, POLICY_FIELDS, "policy setting(s)")
    normalized: dict[str, Any] = {}
    if "cancel_own" in settings:
        cancel_own = settings["cancel_own"]
        if not isinstance(cancel_own, bool):
            raise PolicyError("cancel_own must be a boolean")
        normalized["cancel_own"] = cancel_own
    if "limits" in settings:
        raw_limits = settings["limits"]
        if not isinstance(raw_limits, dict):
            raise PolicyError("limits must be a JSON object")
        _unknown(raw_limits, frozenset(LIMIT_FIELDS), "limit(s)")
        limits: dict[str, int] = {}
        for name in LIMIT_FIELDS:
            if name not in raw_limits:
                continue
            value = _positive_int(raw_limits[name], f"limits.{name}")
            cap = int(getattr(caps, name))
            if value > cap:
                raise PolicyError(
                    f"limits.{name} must not exceed the global limit {cap}"
                )
            limits[name] = value
        if limits:
            normalized["limits"] = limits
    return normalized


def load_policy(
    value: Any,
    caps: VFlowLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Load a policy from storage, clamping legacy or malformed values safely."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    if not isinstance(value, dict):
        value = {}
    policy: dict[str, Any] = {}
    if isinstance(value.get("cancel_own"), bool):
        policy["cancel_own"] = value["cancel_own"]
    raw_limits = value.get("limits")
    if isinstance(raw_limits, dict):
        limits: dict[str, int] = {}
        for name in LIMIT_FIELDS:
            item = raw_limits.get(name)
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                continue
            limits[name] = min(item, int(getattr(caps, name)))
        if limits:
            policy["limits"] = limits
    policy.setdefault("cancel_own", True)
    return policy


def effective_limits(
    value: Any,
    caps: VFlowLimits = DEFAULT_LIMITS,
) -> VFlowLimits:
    """Merge definition limits over global caps (the policy is already capped)."""
    policy = load_policy(value, caps)
    overrides = policy.get("limits")
    if not isinstance(overrides, dict) or not overrides:
        return caps
    values = {name: int(getattr(caps, name)) for name in LIMIT_FIELDS}
    values.update(overrides)
    return VFlowLimits(**values)


def cancel_own(value: Any, caps: VFlowLimits = DEFAULT_LIMITS) -> bool:
    return bool(load_policy(value, caps).get("cancel_own", True))
