"""VFlow: persistent graph workflows for VALERIA."""

from .capabilities import Capability
from .compiler import (
    CompileResult,
    compile_ast,
    compile_graph,
    compile_source,
    graph_from_json,
)
from .models import CompileError, Diagnostic, Edge, Graph, Node, ParseError
from .parser import parse
from .registry import DEFAULT_REGISTRY, NodeRegistry, NodeSpec
from .sdk import register_block

__all__ = [
    "Capability",
    "CompileResult",
    "CompileError",
    "DEFAULT_REGISTRY",
    "Diagnostic",
    "Edge",
    "Graph",
    "Node",
    "NodeRegistry",
    "NodeSpec",
    "ParseError",
    "compile_ast",
    "compile_graph",
    "compile_source",
    "graph_from_json",
    "parse",
    "register_block",
]
