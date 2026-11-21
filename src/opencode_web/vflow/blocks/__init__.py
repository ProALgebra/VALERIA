"""Example code-only VFlow blocks shipped with VALERIA.

Nothing in this package is discovered from user input or loaded dynamically.
An application composition root imports a module and registers its spec and
handler explicitly.
"""

from .example import (
    DEMO_HANDLERS,
    DEMO_REGISTRY,
    ECHO_SPEC,
    EXAMPLE_HANDLERS,
    EXAMPLE_REGISTRY,
    EchoHandler,
    build_example_handlers,
    build_example_registry,
    register_example_block,
)

__all__ = [
    "DEMO_HANDLERS",
    "DEMO_REGISTRY",
    "ECHO_SPEC",
    "EXAMPLE_HANDLERS",
    "EXAMPLE_REGISTRY",
    "EchoHandler",
    "build_example_handlers",
    "build_example_registry",
    "register_example_block",
]
