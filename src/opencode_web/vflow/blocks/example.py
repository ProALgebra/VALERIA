"""Safe example Extension SDK block: ``custom.echo@1.0``.

The handler only reads its immutable planning context and returns one typed
action. It has no database, gateway, network, filesystem or secret access, so
its declared capabilities are exactly ``pure`` and ``deterministic``.
"""

from __future__ import annotations

import copy
from collections.abc import MutableMapping
from typing import Any, ClassVar

from ..actions import Advance, Fail
from ..capabilities import Capability
from ..handlers import DEFAULT_HANDLERS, NodeHandler, PlanContext
from ..registry import BUILTIN_SPECS, NodeRegistry, NodeSpec
from ..sdk import register_block

ECHO = "custom.echo"
ECHO_VERSION = "1.0"
MAX_TAG_LENGTH = 200

ECHO_SPEC = NodeSpec(
    type=ECHO,
    version=ECHO_VERSION,
    title="Echo (example)",
    category="custom",
    description=(
        "Безопасный пример SDK: возвращает вход без изменений либо "
        "оборачивает его в {tag, value}."
    ),
    inputs=("in",),
    outputs=("out",),
    config_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tag": {
                "type": "string",
                "maxLength": MAX_TAG_LENGTH,
                "default": "",
                "description": "Необязательная метка; пустая означает pass-through.",
            }
        },
    },
    capabilities=(Capability.PURE, Capability.DETERMINISTIC),
    call_names=("custom.echo", "echo"),
)


class EchoHandler:
    """Immediate transform-like handler used by SDK examples and tests."""

    node_type: ClassVar[str] = ECHO
    node_version: ClassVar[str] = ECHO_VERSION
    capabilities: ClassVar[tuple[str, ...]] = ECHO_SPEC.capabilities

    def plan(self, context: PlanContext) -> Advance | Fail:
        tag = context.node.config.get("tag", "")
        if not isinstance(tag, str) or len(tag) > MAX_TAG_LENGTH:
            return Fail(
                "ECHO_INVALID_TAG",
                f"Echo tag must be a string of at most {MAX_TAG_LENGTH} characters",
            )
        data: Any = copy.deepcopy(context.point.data)
        if tag:
            data = {"tag": tag, "value": data}
        return Advance(port="out", data=data)


def build_example_registry() -> NodeRegistry:
    """Return a fresh builtin registry plus the example block."""

    return NodeRegistry((*BUILTIN_SPECS, ECHO_SPEC))


def build_example_handlers() -> dict[str, NodeHandler]:
    """Return a fresh builtin handler table plus the example handler."""

    return {**DEFAULT_HANDLERS, ECHO: EchoHandler()}


def register_example_block(
    registry: NodeRegistry,
    handlers: MutableMapping[str, NodeHandler],
) -> NodeSpec:
    """Register the example on a caller-owned registry and handler table."""

    return register_block(registry, handlers, ECHO_SPEC, EchoHandler())


# Demo/test composition only. DEFAULT_REGISTRY deliberately does not include
# the example block: production availability is an explicit deploy decision.
EXAMPLE_REGISTRY = build_example_registry()
EXAMPLE_HANDLERS = build_example_handlers()
DEMO_REGISTRY = EXAMPLE_REGISTRY
DEMO_HANDLERS = EXAMPLE_HANDLERS


__all__ = [
    "DEMO_HANDLERS",
    "DEMO_REGISTRY",
    "ECHO",
    "ECHO_SPEC",
    "ECHO_VERSION",
    "EXAMPLE_HANDLERS",
    "EXAMPLE_REGISTRY",
    "EchoHandler",
    "build_example_handlers",
    "build_example_registry",
    "register_example_block",
]
