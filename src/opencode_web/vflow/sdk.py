"""Code-only registration helper for VFlow extension blocks.

Blocks are ordinary Python modules shipped with VALERIA. There is no dynamic
discovery, import-by-name, plugin upload or user-supplied entry point: an
application composition root calls :func:`register_block` with a trusted
``NodeSpec`` and handler instance at deploy time.
"""

from __future__ import annotations

from collections.abc import MutableMapping

from .capabilities import validate_handler_capabilities
from .handlers import NodeHandler
from .registry import NodeRegistry, NodeSpec


def register_block(
    registry: NodeRegistry,
    handlers: MutableMapping[str, NodeHandler],
    spec: NodeSpec,
    handler: NodeHandler,
) -> NodeSpec:
    """Register one trusted block and bind its handler.

    The call is atomic with respect to both containers: every compatibility
    check runs before either the registry or the handler mapping is mutated.
    """

    if not isinstance(spec, NodeSpec):
        raise TypeError("VFlow block spec must be a NodeSpec instance")
    if not callable(getattr(handler, "plan", None)):
        raise TypeError("VFlow block handler must provide a callable plan() method")

    handler_type = getattr(handler, "node_type", None)
    if handler_type != spec.type:
        raise ValueError(
            f"VFlow handler node_type {handler_type!r} does not match "
            f"NodeSpec type {spec.type!r}"
        )
    handler_version = getattr(handler, "node_version", None)
    if handler_version is not None and str(handler_version) != spec.version:
        raise ValueError(
            f"VFlow handler version {handler_version!r} does not match "
            f"NodeSpec version {spec.version!r}"
        )
    validate_handler_capabilities(spec, handler)

    if spec.type in handlers:
        raise ValueError(
            f"VFlow handler already registered for type {spec.type!r}"
        )

    registry.register(spec)
    handlers[spec.type] = handler
    return spec


__all__ = ["register_block"]
