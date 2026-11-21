"""VFlow compiler: AST -> immutable graph IR with structural validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import (
    CompileError,
    Diagnostic,
    Edge,
    Graph,
    Node,
)
from .parser import Endpoint, Identifier, StepAST, WorkflowAST, parse
from .registry import (
    DEFAULT_REGISTRY,
    FORK,
    IF,
    MERGE,
    TASK,
    TRANSFORM,
    NodeRegistry,
    NodeSpec,
)

IF_OPERATORS = {
    "eq", "ne", "gt", "ge", "lt", "le",
    "contains", "in", "exists", "is_null",
}
MISSING_MODES = {"error", "false", "true"}
RESULT_MODES = {"auto", "strict_json", "none"}
MAX_FORK_N = 32


@dataclass(frozen=True)
class _ScopeFrame:
    """One open Fork/Merge pair while walking a graph path."""

    fork_node_id: str
    merge_node_id: str
    branch_index: int


@dataclass(frozen=True)
class CompileResult:
    graph: Graph
    source: str
    graph_hash: str
    required_handlers: tuple[str, ...]


def compile_source(source: str, registry: NodeRegistry = DEFAULT_REGISTRY) -> CompileResult:
    return compile_ast(parse(source), source=source, registry=registry)


def compile_ast(
    ast: WorkflowAST,
    *,
    source: str = "",
    registry: NodeRegistry = DEFAULT_REGISTRY,
) -> CompileResult:
    diagnostics: list[Diagnostic] = []
    nodes_by_id: dict[str, Node] = {}
    steps_by_id: dict[str, StepAST] = {}

    for step in ast.steps:
        if step.name in steps_by_id:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_DUPLICATE_STEP",
                    f"Step {step.name!r} is declared more than once",
                    line=step.line,
                    column=step.column,
                    node_id=step.name,
                )
            )
            continue
        steps_by_id[step.name] = step
        node = _compile_step(step, diagnostics, registry)
        if node is not None:
            nodes_by_id[step.name] = node

    if not nodes_by_id:
        diagnostics.append(
            Diagnostic("COMPILE_EMPTY_WORKFLOW", "Workflow has no executable steps")
        )

    edges, entry = _compile_edges(ast, nodes_by_id, diagnostics)
    _validate_nodes(nodes_by_id, steps_by_id, edges, diagnostics, registry)
    _validate_reachability(entry, nodes_by_id, edges, diagnostics)

    if diagnostics:
        raise CompileError(diagnostics)

    graph = Graph(
        workflow=ast.name,
        entry=entry,
        nodes=tuple(nodes_by_id[step.name] for step in ast.steps if step.name in nodes_by_id),
        edges=tuple(edges),
    )
    canonical = graph.canonical_json()
    graph_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    required_handlers = tuple(
        sorted({f"{node.type}@{node.type_version}" for node in graph.nodes})
    )
    return CompileResult(
        graph=graph,
        source=source,
        graph_hash=graph_hash,
        required_handlers=required_handlers,
    )


def _unwrap(value: Any) -> Any:
    if isinstance(value, Identifier):
        return value.value
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _unwrap(item) for key, item in value.items()}
    return value


def _compile_step(
    step: StepAST,
    diagnostics: list[Diagnostic],
    registry: NodeRegistry,
) -> Node | None:
    spec = registry.resolve_call(step.dsl_type)
    if spec is None:
        diagnostics.append(
            Diagnostic(
                "COMPILE_UNKNOWN_NODE_TYPE",
                f"Unknown VFlow node type {step.dsl_type!r}",
                line=step.line,
                column=step.column,
                node_id=step.name,
            )
        )
        return None

    args = {key: _unwrap(value) for key, value in step.args.items()}
    config = _compile_config(spec, args, step, diagnostics)
    if config is None:
        return None
    return Node(
        id=step.name,
        type=spec.type,
        type_version=spec.version,
        config=config,
    )


def _compile_config(
    spec: NodeSpec,
    args: dict[str, Any],
    step: StepAST,
    diagnostics: list[Diagnostic],
) -> dict[str, Any] | None:
    type_id = spec.type

    def error(code: str, message: str) -> None:
        diagnostics.append(
            Diagnostic(code, message, line=step.line, column=step.column, node_id=step.name)
        )

    if type_id == TASK:
        unknown = set(args) - {
            "task_id", "input", "input_path", "result_mode",
            "queue_timeout_seconds", "execution_timeout_seconds", "retries",
        }
        if unknown:
            error("COMPILE_UNKNOWN_ARGUMENT", f"Unknown task argument(s): {sorted(unknown)}")
            return None
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            error("COMPILE_TASK_ID_REQUIRED", "task.run requires a non-empty task_id")
            return None
        result_mode = args.get("result_mode", "auto")
        if result_mode not in RESULT_MODES:
            error("COMPILE_INVALID_RESULT_MODE", f"Invalid result_mode: {result_mode!r}")
            return None
        config: dict[str, Any] = {
            "task_id": task_id,
            "input_path": str(args.get("input_path", args.get("input", "$"))),
            "result_mode": result_mode,
        }
        for key in ("queue_timeout_seconds", "execution_timeout_seconds", "retries"):
            if key in args:
                value = args[key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    error("COMPILE_INVALID_ARGUMENT", f"{key} must be a non-negative integer")
                    return None
                config[key] = value
        return config

    if type_id == TRANSFORM:
        unknown = set(args) - {"operations", "create_missing"}
        if unknown:
            error("COMPILE_UNKNOWN_ARGUMENT", f"Unknown transform argument(s): {sorted(unknown)}")
            return None
        operations = args.get("operations")
        if not isinstance(operations, list) or not operations:
            error("COMPILE_TRANSFORM_OPERATIONS_REQUIRED", "transform requires operations")
            return None
        create_missing = args.get("create_missing", False)
        if not isinstance(create_missing, bool):
            error("COMPILE_INVALID_ARGUMENT", "create_missing must be a boolean")
            return None
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                error(
                    "COMPILE_INVALID_TRANSFORM_OPERATION",
                    f"Operation #{index} must be an object",
                )
                return None
            if not isinstance(operation.get("op"), str):
                error(
                    "COMPILE_INVALID_TRANSFORM_OPERATION",
                    f"Operation #{index} requires an op name",
                )
                return None
        return {"operations": operations, "create_missing": create_missing}

    if type_id == IF:
        unknown = set(args) - {"path", "operator", "value", "missing"}
        if unknown:
            error("COMPILE_UNKNOWN_ARGUMENT", f"Unknown if argument(s): {sorted(unknown)}")
            return None
        path = args.get("path")
        if not isinstance(path, str) or not path:
            error("COMPILE_IF_PATH_REQUIRED", "if requires a path")
            return None
        operator = args.get("operator")
        if operator not in IF_OPERATORS:
            error("COMPILE_INVALID_IF_OPERATOR", f"Invalid if operator: {operator!r}")
            return None
        missing = args.get("missing", "error")
        if missing not in MISSING_MODES:
            error("COMPILE_INVALID_MISSING_MODE", f"Invalid missing mode: {missing!r}")
            return None
        config = {"path": path, "operator": operator, "missing": missing}
        if "value" in args:
            config["value"] = args["value"]
        if operator not in {"exists", "is_null"} and "value" not in config:
            error("COMPILE_IF_VALUE_REQUIRED", f"if operator {operator!r} requires a value")
            return None
        return config

    if type_id == FORK:
        unknown = set(args) - {"count", "join"}
        if unknown:
            error("COMPILE_UNKNOWN_ARGUMENT", f"Unknown fork argument(s): {sorted(unknown)}")
            return None
        count = args.get("count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 2
            or count > MAX_FORK_N
        ):
            error("COMPILE_INVALID_FORK_COUNT", f"fork count must be between 2 and {MAX_FORK_N}")
            return None
        join = args.get("join")
        if not isinstance(join, str) or not join:
            error("COMPILE_FORK_JOIN_REQUIRED", "fork requires join: <merge step>")
            return None
        return {"count": count, "join": join}

    if type_id == MERGE:
        unknown = set(args) - {"mode"}
        if unknown:
            error("COMPILE_UNKNOWN_ARGUMENT", f"Unknown merge argument(s): {sorted(unknown)}")
            return None
        mode = args.get("mode", "all")
        if mode != "all":
            error("COMPILE_UNSUPPORTED_MERGE_MODE", "VFlow v1 supports only merge mode 'all'")
            return None
        return {"mode": "all"}

    # Code-registered custom types are trusted registry metadata. Their args
    # become config verbatim; structural validation happens after compilation
    # and runtime execution still requires a matching code-registered handler.
    return dict(args)


def _compile_edges(
    ast: WorkflowAST,
    nodes: dict[str, Node],
    diagnostics: list[Diagnostic],
) -> tuple[list[Edge], str]:
    edges: list[Edge] = []
    entry: str | None = None
    seen: set[tuple[str, str, str]] = set()

    for statement in ast.edges:
        endpoints = statement.endpoints
        for index in range(len(endpoints) - 1):
            source = endpoints[index]
            target = endpoints[index + 1]
            if source.name == "end":
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_EDGE_FROM_END",
                        "The end marker cannot have outgoing edges",
                        line=source.line,
                        column=source.column,
                    )
                )
                continue
            if target.name == "start":
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_EDGE_TO_START",
                        "The start marker cannot have incoming edges",
                        line=target.line,
                        column=target.column,
                    )
                )
                continue
            if target.name not in nodes and target.name != "end":
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_UNKNOWN_TARGET",
                        f"Unknown edge target: {target.name!r}",
                        line=target.line,
                        column=target.column,
                        node_id=target.name,
                    )
                )
                continue
            if source.name != "start" and source.name not in nodes:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_UNKNOWN_SOURCE",
                        f"Unknown edge source: {source.name!r}",
                        line=source.line,
                        column=source.column,
                        node_id=source.name,
                    )
                )
                continue
            if target.port is not None or target.index is not None:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_TARGET_HAS_PORT",
                        "Edge target must be a node or end, without a port",
                        line=target.line,
                        column=target.column,
                    )
                )
                continue

            if source.name == "start":
                if source.port is not None or source.index is not None:
                    diagnostics.append(
                        Diagnostic(
                            "COMPILE_START_HAS_PORT",
                            "The start marker has no ports",
                            line=source.line,
                            column=source.column,
                        )
                    )
                    continue
                if entry is not None:
                    diagnostics.append(
                        Diagnostic(
                            "COMPILE_MULTIPLE_START_EDGES",
                            "Workflow must have exactly one outgoing start edge",
                            line=source.line,
                            column=source.column,
                        )
                    )
                else:
                    entry = target.name
                source_port = "out"
            else:
                source_port = _source_port(source)

            key = (source.name, source_port, target.name)
            if key in seen:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_DUPLICATE_EDGE",
                        f"Duplicate edge {source.name}.{source_port} -> {target.name}",
                        line=statement.line,
                        column=statement.column,
                    )
                )
                continue
            seen.add(key)
            edges.append(Edge(source=source.name, source_port=source_port, target=target.name))

    if entry is None:
        diagnostics.append(
            Diagnostic("COMPILE_MISSING_ENTRY", "Workflow has no start -> ... edge")
        )
    if not any(edge.target == "end" for edge in edges):
        diagnostics.append(
            Diagnostic("COMPILE_MISSING_END", "Workflow has no edge to end")
        )
    return edges, entry or ""


def _source_port(endpoint: Endpoint) -> str:
    if endpoint.index is not None:
        return f"branch[{endpoint.index}]"
    if endpoint.port is not None:
        return endpoint.port
    return "out"


def _validate_nodes(
    nodes: dict[str, Node],
    steps: dict[str, StepAST],
    edges: list[Edge],
    diagnostics: list[Diagnostic],
    registry: NodeRegistry,
) -> None:
    outgoing: dict[tuple[str, str], list[Edge]] = {}
    for edge in edges:
        outgoing.setdefault((edge.source, edge.source_port), []).append(edge)

    for node in nodes.values():
        spec = registry.get(node.type, node.type_version)
        if spec is None:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_UNKNOWN_HANDLER_VERSION",
                    f"Unknown handler version {node.type}@{node.type_version}",
                    node_id=node.id,
                )
            )
            continue

        if node.type == IF:
            for port in ("true", "false"):
                port_edges = outgoing.get((node.id, port), [])
                if len(port_edges) != 1:
                    diagnostics.append(
                        Diagnostic(
                            "COMPILE_IF_PORT_COUNT",
                            f"If node {node.id!r} must have exactly one {port!r} edge",
                            node_id=node.id,
                        )
                    )
            for (source, port), _port_edges in outgoing.items():
                if source == node.id and port not in ("true", "false"):
                    diagnostics.append(
                        Diagnostic(
                            "COMPILE_IF_UNKNOWN_PORT",
                            f"If node {node.id!r} has unsupported output port {port!r}",
                            node_id=node.id,
                        )
                    )
            continue

        if node.type == FORK:
            count = int(node.config["count"])
            join = str(node.config["join"])
            merge_node = nodes.get(join)
            if merge_node is None or merge_node.type != MERGE:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_FORK_JOIN_NOT_MERGE",
                        f"Fork {node.id!r} joins to non-merge node {join!r}",
                        node_id=node.id,
                    )
                )
            branch_ports = {f"branch[{index}]" for index in range(count)}
            actual_ports = {
                port for (source, port), values in outgoing.items()
                if source == node.id and values
            }
            if actual_ports == {"all"}:
                pass
            elif actual_ports != branch_ports:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_FORK_PORTS",
                        f"Fork {node.id!r} must have exactly branch[0..{count - 1}] "
                        "or one 'all' output",
                        node_id=node.id,
                    )
                )
            for port in actual_ports:
                if port != "all" and port not in branch_ports:
                    diagnostics.append(
                        Diagnostic(
                            "COMPILE_FORK_UNKNOWN_PORT",
                            f"Fork {node.id!r} has unsupported output port {port!r}",
                            node_id=node.id,
                        )
                    )
            if len(outgoing.get((node.id, "out"), [])) > 0:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_FORK_USES_OUT",
                        f"Fork {node.id!r} must use branch ports, not 'out'",
                        node_id=node.id,
                    )
                )
            continue

        # Task, Transform and Merge have exactly one 'out' edge and no fan-out.
        for (source, port), port_edges in outgoing.items():
            if source != node.id:
                continue
            if port != "out":
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_UNEXPECTED_PORT",
                        f"Node {node.id!r} does not have output port {port!r}",
                        node_id=node.id,
                    )
                )
            elif len(port_edges) != 1:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_FAN_OUT",
                        f"Node {node.id!r} has {len(port_edges)} outgoing edges; "
                        "use Fork for parallel branches",
                        node_id=node.id,
                    )
                )
        if not any(
            edge.source == node.id and edge.source_port == "out" for edge in edges
        ):
            diagnostics.append(
                Diagnostic(
                    "COMPILE_MISSING_OUTPUT",
                    f"Node {node.id!r} has no outgoing 'out' edge",
                    node_id=node.id,
                )
            )

    # A Merge is paired with at most one Fork in v1. Nested scopes are checked
    # by walking every possible path with an explicit open-scope stack.
    joins: dict[str, str] = {}
    paired_merges: set[str] = set()
    for node in nodes.values():
        if node.type != FORK:
            continue
        join = str(node.config["join"])
        if join in joins:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_MERGE_REUSED",
                    f"Merge {join!r} is already paired with Fork {joins[join]!r}",
                    node_id=node.id,
                )
            )
        joins[join] = node.id
        merge = nodes.get(join)
        if merge is not None and merge.type == MERGE:
            paired_merges.add(join)

    for node in nodes.values():
        if node.type == MERGE and node.id not in paired_merges:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_MERGE_UNPAIRED",
                    f"Merge {node.id!r} is not the join of any Fork",
                    node_id=node.id,
                )
            )

    _validate_fork_scopes(nodes, edges, diagnostics)


def _fork_branch_edges(
    node: Node, outgoing: list[Edge]
) -> list[tuple[int, Edge]]:
    """Return ``(branch_index, edge)`` pairs for a Fork's configured ports."""
    count = int(node.config.get("count", 0))
    all_edges = [edge for edge in outgoing if edge.source_port == "all"]
    if all_edges:
        return [(index, all_edges[0]) for index in range(count)]
    by_port = {edge.source_port: edge for edge in outgoing}
    return [
        (index, by_port[f"branch[{index}]"])
        for index in range(count)
        if f"branch[{index}]" in by_port
    ]


def _validate_fork_scopes(
    nodes: dict[str, Node],
    edges: list[Edge],
    diagnostics: list[Diagnostic],
) -> None:
    """Reject paths which bypass or cross an open Fork/Merge scope.

    The walker keeps a stack of Fork frames. A Merge must close the innermost
    frame, an ``end`` is only valid with an empty stack, and a Fork cannot be
    re-entered while its previous instance is still open. Every branch must
    have a route to its paired Merge, including both outcomes of an If.
    """
    outgoing: dict[str, list[Edge]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source, []).append(edge)

    reported: set[tuple[str, str | None]] = {
        (item.code, item.node_id) for item in diagnostics
    }

    def report(code: str, message: str, node_id: str) -> None:
        key = (code, node_id)
        if key in reported:
            return
        reported.add(key)
        diagnostics.append(Diagnostic(code, message, node_id=node_id))

    def transitions(
        node_id: str, stack: tuple[_ScopeFrame, ...]
    ) -> list[tuple[str, tuple[_ScopeFrame, ...]]]:
        if node_id == "end":
            if stack:
                frame = stack[-1]
                report(
                    "COMPILE_FORK_BYPASS_MERGE",
                    f"Path reaches end while Fork {frame.fork_node_id!r} is open; "
                    f"expected Merge {frame.merge_node_id!r}",
                    frame.fork_node_id,
                )
            return []
        node = nodes.get(node_id)
        if node is None:
            return []
        if node.type == MERGE:
            if not stack:
                report(
                    "COMPILE_MERGE_WITHOUT_FORK",
                    f"Merge {node.id!r} is reachable without an open Fork",
                    node.id,
                )
                return []
            frame = stack[-1]
            if frame.merge_node_id != node.id:
                report(
                    "COMPILE_MERGE_SCOPE_MISMATCH",
                    f"Merge {node.id!r} is reached while Fork "
                    f"{frame.fork_node_id!r} must merge at {frame.merge_node_id!r}",
                    node.id,
                )
                return []
            stack = stack[:-1]
        elif node.type == FORK:
            if any(frame.fork_node_id == node.id for frame in stack):
                report(
                    "COMPILE_FORK_REENTRY",
                    f"Fork {node.id!r} is re-entered before its Merge "
                    f"{node.config.get('join')!r}",
                    node.id,
                )
                return []
            join = str(node.config.get("join") or "")
            return [
                (
                    edge.target,
                    (*stack, _ScopeFrame(node.id, join, branch_index)),
                )
                for branch_index, edge in _fork_branch_edges(
                    node, outgoing.get(node.id, [])
                )
            ]
        return [(edge.target, stack) for edge in outgoing.get(node.id, [])]

    # First pass: verify scope discipline on every path reachable from entry.
    entry = next(
        (edge.target for edge in edges if edge.source == "start"),
        "",
    )
    if entry:
        seen: set[tuple[str, tuple[_ScopeFrame, ...]]] = set()
        pending = [(entry, ())]
        while pending:
            state = pending.pop()
            if state in seen:
                continue
            seen.add(state)
            pending.extend(transitions(*state))

    # Second pass: every branch needs at least one route to its own Merge.
    for node in nodes.values():
        if node.type != FORK:
            continue
        join = str(node.config.get("join") or "")
        merge = nodes.get(join)
        if merge is None or merge.type != MERGE:
            continue
        for branch_index, edge in _fork_branch_edges(
            node, outgoing.get(node.id, [])
        ):
            frame = _ScopeFrame(node.id, join, branch_index)
            if not _scope_reaches_merge(edge.target, frame, transitions):
                report(
                    "COMPILE_FORK_BRANCH_UNJOINED",
                    f"Branch {branch_index} of Fork {node.id!r} has no route "
                    f"to Merge {join!r}",
                    node.id,
                )


def _scope_reaches_merge(
    start: str,
    frame: _ScopeFrame,
    transitions: Any,
) -> bool:
    """Return whether ``start`` can close exactly ``frame`` at its Merge."""
    seen: set[tuple[str, tuple[_ScopeFrame, ...]]] = set()
    pending: list[tuple[str, tuple[_ScopeFrame, ...]]] = [(start, (frame,))]
    while pending:
        node_id, stack = pending.pop()
        if node_id == frame.merge_node_id and stack and stack[-1] == frame:
            return True
        state = (node_id, stack)
        if state in seen:
            continue
        seen.add(state)
        pending.extend(transitions(node_id, stack))
    return False


def _validate_reachability(
    entry: str,
    nodes: dict[str, Node],
    edges: list[Edge],
    diagnostics: list[Diagnostic],
) -> None:
    if not entry:
        return
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
    seen: set[str] = set()
    stack = [entry]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, []))
    missing = sorted(set(nodes) - seen)
    if missing:
        diagnostics.append(
            Diagnostic(
                "COMPILE_UNREACHABLE_NODES",
                f"Nodes are unreachable from start: {', '.join(missing)}",
            )
        )
    if "end" not in seen:
        diagnostics.append(
            Diagnostic(
                "COMPILE_END_UNREACHABLE",
                "The end marker is unreachable from start",
            )
        )


def compile_graph(
    value: str | dict[str, Any] | Graph,
    *,
    registry: NodeRegistry = DEFAULT_REGISTRY,
    source: str = "",
) -> CompileResult:
    """Validate an IR graph produced by the canvas or another trusted compiler.

    The canvas sends IR directly, so it must pass through the same structural
    checks as the textual compiler. Node configs are normalized exactly like
    their DSL equivalents before the immutable graph is hashed.
    """

    graph = value if isinstance(value, Graph) else graph_from_json(value)
    diagnostics: list[Diagnostic] = []
    if graph.ir_version != 1:
        diagnostics.append(
            Diagnostic(
                "COMPILE_UNSUPPORTED_IR_VERSION",
                f"Unsupported IR version: {graph.ir_version}",
            )
        )
    if not graph.workflow.strip():
        diagnostics.append(
            Diagnostic("COMPILE_WORKFLOW_NAME_REQUIRED", "Workflow name is required")
        )

    nodes_by_id: dict[str, Node] = {}
    for node in graph.nodes:
        if node.id in nodes_by_id:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_DUPLICATE_STEP",
                    f"Node {node.id!r} is declared more than once",
                    node_id=node.id,
                )
            )
            continue
        if node.id in {"start", "end"}:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_RESERVED_NODE_ID",
                    f"Node id {node.id!r} is reserved",
                    node_id=node.id,
                )
            )
            continue
        spec = registry.get(node.type, node.type_version)
        if spec is None:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_UNKNOWN_HANDLER_VERSION",
                    f"No handler registered for {node.type}@{node.type_version}",
                    node_id=node.id,
                )
            )
            continue
        fake_step = StepAST(
            name=node.id,
            dsl_type=node.type,
            args=dict(node.config),
            line=1,
            column=1,
        )
        config = _compile_config(spec, dict(node.config), fake_step, diagnostics)
        if config is not None:
            nodes_by_id[node.id] = Node(
                id=node.id,
                type=node.type,
                type_version=spec.version,
                config=config,
                ui=node.ui,
            )

    edges, entry = _validate_graph_edges(graph, nodes_by_id, diagnostics)
    _validate_nodes(nodes_by_id, {}, edges, diagnostics, registry)
    _validate_reachability(entry, nodes_by_id, edges, diagnostics)

    if diagnostics:
        raise CompileError(diagnostics)

    normalized = Graph(
        workflow=graph.workflow.strip(),
        entry=entry,
        nodes=tuple(
            nodes_by_id[node.id] for node in graph.nodes if node.id in nodes_by_id
        ),
        edges=tuple(edges),
        ir_version=1,
    )
    canonical = normalized.canonical_json()
    graph_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    required_handlers = tuple(
        sorted({f"{node.type}@{node.type_version}" for node in normalized.nodes})
    )
    return CompileResult(
        graph=normalized,
        source=source,
        graph_hash=graph_hash,
        required_handlers=required_handlers,
    )


def _validate_graph_edges(
    graph: Graph,
    nodes: dict[str, Node],
    diagnostics: list[Diagnostic],
) -> tuple[list[Edge], str]:
    edges: list[Edge] = []
    entry: str | None = None
    seen: set[tuple[str, str, str]] = set()

    for edge in graph.edges:
        if edge.source == "end":
            diagnostics.append(
                Diagnostic(
                    "COMPILE_EDGE_FROM_END",
                    "The end marker cannot have outgoing edges",
                    edge_id=f"{edge.source}:{edge.target}",
                )
            )
            continue
        if edge.target == "start":
            diagnostics.append(
                Diagnostic(
                    "COMPILE_EDGE_TO_START",
                    "The start marker cannot have incoming edges",
                    edge_id=f"{edge.source}:{edge.target}",
                )
            )
            continue
        if edge.source != "start" and edge.source not in nodes:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_UNKNOWN_SOURCE",
                    f"Unknown edge source: {edge.source!r}",
                    node_id=edge.source,
                )
            )
            continue
        if edge.target != "end" and edge.target not in nodes:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_UNKNOWN_TARGET",
                    f"Unknown edge target: {edge.target!r}",
                    node_id=edge.target,
                )
            )
            continue
        if edge.source == "start":
            if edge.source_port != "out":
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_START_HAS_PORT",
                        "The start marker has no ports",
                    )
                )
                continue
            if entry is not None:
                diagnostics.append(
                    Diagnostic(
                        "COMPILE_MULTIPLE_START_EDGES",
                        "Workflow must have exactly one outgoing start edge",
                    )
                )
            else:
                entry = edge.target
        key = (edge.source, edge.source_port, edge.target)
        if key in seen:
            diagnostics.append(
                Diagnostic(
                    "COMPILE_DUPLICATE_EDGE",
                    f"Duplicate edge {edge.source}.{edge.source_port} -> {edge.target}",
                )
            )
            continue
        seen.add(key)
        edges.append(edge)

    if entry is None:
        diagnostics.append(
            Diagnostic("COMPILE_MISSING_ENTRY", "Workflow has no start -> ... edge")
        )
    elif graph.entry and graph.entry != entry:
        diagnostics.append(
            Diagnostic(
                "COMPILE_ENTRY_MISMATCH",
                f"Graph entry {graph.entry!r} does not match start edge target {entry!r}",
            )
        )
    if not any(edge.target == "end" for edge in edges):
        diagnostics.append(Diagnostic("COMPILE_MISSING_END", "Workflow has no edge to end"))
    return edges, entry or ""


def graph_from_json(value: str | dict[str, Any]) -> Graph:
    data = json.loads(value) if isinstance(value, str) else value
    return Graph.from_dict(data)
