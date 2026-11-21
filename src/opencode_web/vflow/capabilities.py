"""Declarative capability model for code-only VFlow blocks.

Capabilities do not grant access by themselves: handlers never receive a
database connection, gateway, network client or filesystem object. They are a
review and drift-control mechanism which makes a block's intended side effects
explicit in ``NodeSpec`` and in the registry manifest. A handler that needs a
new capability must be changed and reviewed together with its spec; unknown
capability names are rejected at registration time.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    """Supported capability tokens.

    ``pure`` means the handler only transforms its immutable input snapshot.
    ``deterministic`` means the same input and config always produce the same
    output. ``external_io`` covers network, filesystem, database, gateway and
    queue integration. ``secrets`` covers reading credentials or tokens.
    ``workspace`` covers delegated access to a VALERIA workspace. ``queue`` is
    the built-in VALERIA task-queue marker.
    """

    PURE = "pure"
    DETERMINISTIC = "deterministic"
    EXTERNAL_IO = "external_io"
    SECRETS = "secrets"
    WORKSPACE = "workspace"
    QUEUE = "queue"


ALL_CAPABILITIES: frozenset[str] = frozenset(item.value for item in Capability)
CAPABILITIES = ALL_CAPABILITIES
PURE = Capability.PURE.value
DETERMINISTIC = Capability.DETERMINISTIC.value
EXTERNAL_IO = Capability.EXTERNAL_IO.value
SECRETS = Capability.SECRETS.value
WORKSPACE = Capability.WORKSPACE.value
QUEUE = Capability.QUEUE.value
PURE_CAPABILITIES: frozenset[str] = frozenset(
    {Capability.PURE.value, Capability.DETERMINISTIC.value}
)
NON_PURE_CAPABILITIES: frozenset[str] = ALL_CAPABILITIES - PURE_CAPABILITIES

CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    Capability.PURE.value: "No side effects; only the immutable input snapshot is read.",
    Capability.DETERMINISTIC.value: "Same input and config produce the same output.",
    Capability.EXTERNAL_IO.value: "Network, filesystem, database, gateway or queue access.",
    Capability.SECRETS.value: "Reads credentials, tokens or other secret material.",
    Capability.WORKSPACE.value: "Uses a delegated VALERIA workspace.",
    Capability.QUEUE.value: "Schedules work through the VALERIA task queue.",
}


def normalize_capabilities(
    capabilities: Iterable[str] | None,
    *,
    type_id: str = "",
) -> tuple[str, ...]:
    """Return a validated, de-duplicated tuple of capability names.

    Unknown tokens, duplicates and the contradictory combination of ``pure``
    with any side-effecting capability are rejected. A plain string is treated
    as one capability, not as an iterable of characters.
    """

    if capabilities is None:
        return ()
    if isinstance(capabilities, str):
        capabilities = (capabilities,)

    normalized: list[str] = []
    for value in capabilities:
        if isinstance(value, Capability):
            token = value.value
        elif isinstance(value, str):
            token = value
        else:
            raise TypeError(
                f"VFlow capability for {type_id or '<unknown>'} must be a string"
            )
        if token not in ALL_CAPABILITIES:
            raise ValueError(
                f"Unknown VFlow capability {token!r} for {type_id or '<unknown>'}"
            )
        if token in normalized:
            raise ValueError(
                f"Duplicate VFlow capability {token!r} for {type_id or '<unknown>'}"
            )
        normalized.append(token)

    if (
        Capability.PURE.value in normalized
        and NON_PURE_CAPABILITIES.intersection(normalized)
    ):
        raise ValueError(
            f"VFlow capability 'pure' cannot be combined with side effects "
            f"for {type_id or '<unknown>'}"
        )
    return tuple(normalized)


def validate_capabilities(
    capabilities: Iterable[str] | None,
    *,
    type_id: str = "",
) -> tuple[str, ...]:
    """Public alias used by NodeSpec and registration helpers."""

    return normalize_capabilities(capabilities, type_id=type_id)


def validate_handler_capabilities(spec: Any, handler: Any) -> None:
    """Reject drift between a handler and its registered ``NodeSpec``.

    Handlers may omit ``capabilities`` for backwards compatibility, but when
    they declare it the declaration must match the spec exactly.
    """

    declared = getattr(handler, "capabilities", None)
    if declared is None:
        return
    normalized = normalize_capabilities(declared, type_id=str(getattr(spec, "type", "")))
    if set(normalized) != set(getattr(spec, "capabilities", ())):
        raise ValueError(
            "VFlow handler capabilities do not match NodeSpec for "
            f"{getattr(spec, 'type', '<unknown>')}@{getattr(spec, 'version', '?')}"
        )


def has_capability(spec: Any, capability: Capability | str) -> bool:
    """Return whether a spec-like object declares ``capability``."""

    token = capability.value if isinstance(capability, Capability) else str(capability)
    return token in set(getattr(spec, "capabilities", ()))


__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITIES",
    "CAPABILITY_DESCRIPTIONS",
    "DETERMINISTIC",
    "EXTERNAL_IO",
    "NON_PURE_CAPABILITIES",
    "PURE",
    "PURE_CAPABILITIES",
    "QUEUE",
    "SECRETS",
    "WORKSPACE",
    "Capability",
    "has_capability",
    "normalize_capabilities",
    "validate_capabilities",
    "validate_handler_capabilities",
]
