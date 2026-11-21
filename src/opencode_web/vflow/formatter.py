"""Canonical graph IR to VFlow source formatter."""

from __future__ import annotations

import json
from typing import Any

from .models import Edge, Graph, Node
from .registry import FORK, IF, MERGE, TASK, TRANSFORM

_TYPE_NAMES = {
    TASK: "task.run",
    TRANSFORM: "transform",
    IF: "if",
    FORK: "fork",
    MERGE: "merge",
}


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _config(node: Node) -> str:
    items = []
    for key, value in node.config.items():
        if key == "value" and node.type == IF:
            items.append(f"value: {_value(value)}")
        else:
            items.append(f"{key}: {_value(value)}")
    return ", ".join(items)


def _endpoint(edge: Edge) -> str:
    port = edge.source_port
    if port == "out":
        return edge.source
    if port in {"true", "false"}:
        return f"{edge.source}.{port}"
    if port.startswith("branch[") and port.endswith("]"):
        return f"{edge.source}[{port[7:-1]}]"
    return f"{edge.source}.{port}"


def format_graph(graph: Graph) -> str:
    """Render a graph as valid, deterministic VFlow source."""
    lines = [
        f"workflow {_value(graph.workflow)} {{",
        "  version: 1;",
        "  input: json;",
    ]
    for node in graph.nodes:
        type_name = _TYPE_NAMES.get(node.type)
        if type_name is None:
            raise ValueError(f"Unsupported node type: {node.type}")
        lines.append(f"  step {node.id} = {type_name}({_config(node)});")
    for edge in graph.edges:
        lines.append(f"  {_endpoint(edge)} -> {edge.target};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def format_graph_dict(value: dict[str, Any]) -> str:
    return format_graph(Graph.from_dict(value))
