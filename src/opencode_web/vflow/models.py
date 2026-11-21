"""Serializable VFlow graph IR and shared diagnostics.

The IR is deliberately plain JSON: it is stored in SQLite, hashed on publish
and must remain readable by older runtimes. Runtime state never belongs here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

IR_VERSION = 1


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    type_version: str
    config: dict[str, Any]
    ui: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "type_version": self.type_version,
            "config": self.config,
            "ui": self.ui,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Node:
        return cls(
            id=str(value["id"]),
            type=str(value["type"]),
            type_version=str(value.get("type_version", "1.0")),
            config=dict(value.get("config") or {}),
            ui=dict(value.get("ui") or {}),
        )


@dataclass(frozen=True)
class Edge:
    source: str
    source_port: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "source_port": self.source_port,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Edge:
        return cls(
            source=str(value["source"]),
            source_port=str(value.get("source_port", "out")),
            target=str(value["target"]),
        )


@dataclass(frozen=True)
class Graph:
    workflow: str
    entry: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    ir_version: int = IR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_version": self.ir_version,
            "workflow": self.workflow,
            "entry": self.entry,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Graph:
        return cls(
            workflow=str(value["workflow"]),
            entry=str(value["entry"]),
            nodes=tuple(Node.from_dict(item) for item in value.get("nodes", [])),
            edges=tuple(Edge.from_dict(item) for item in value.get("edges", [])),
            ir_version=int(value.get("ir_version", IR_VERSION)),
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    line: int | None = None
    column: int | None = None
    node_id: str | None = None
    edge_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
        }


class VFlowError(Exception):
    """Base error carrying machine-readable diagnostics."""

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__(diagnostics[0].message if diagnostics else "VFlow error")

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostics": [item.to_dict() for item in self.diagnostics]}


class ParseError(VFlowError):
    pass


class CompileError(VFlowError):
    pass


class ValidationError(VFlowError):
    pass
