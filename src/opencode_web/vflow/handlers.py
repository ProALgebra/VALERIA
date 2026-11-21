"""Pure planning handlers for the built-in VFlow node types.

A handler receives a read-only :class:`PlanContext` and returns one typed
action. It never receives a database connection, a task gateway or an async
primitive, so planning cannot leak persistence side effects.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from .actions import (
    Action,
    Advance,
    ArriveMerge,
    EnqueueTask,
    Fail,
    SpawnFork,
    Suspend,
)
from .capabilities import Capability
from .logic import MISSING, apply_transform, evaluate_if, get_path
from .models import Graph, Node
from .registry import FORK, IF, MERGE, TASK, TRANSFORM

TASK_POLL_SECONDS = 2
TASK_WAIT_KIND = "task"
TASK_RETRY_KIND = "task_retry"
TASK_WAITING_STATES = {
    "queued",
    "dispatching",
    "running",
    "waiting_permission",
    "waiting_question",
}
# Retrying these outcomes cannot succeed: the Task version changed, the run
# was cancelled, or the output already violated the node contract.
TASK_NON_RETRYABLE_CODES = {"TASK_CHANGED", "TASK_UNAVAILABLE", "TASK_CANCELLED"}


@dataclass(frozen=True)
class PointView:
    """Immutable runtime snapshot exposed to a handler."""

    id: str
    run_id: str
    node_id: str
    data: Any
    fork_stack: tuple[dict[str, Any], ...]
    visit_seq: int
    attempt: int
    task_run_id: str | None


@dataclass(frozen=True)
class PlanContext:
    """Everything a handler may inspect while planning an action."""

    node: Node
    point: PointView
    graph: Graph
    task_status: dict[str, Any] | None = None


class NodeHandler(Protocol):
    """Contract implemented by every code-registered node type."""

    node_type: ClassVar[str]
    capabilities: ClassVar[tuple[str, ...]]

    def plan(self, context: PlanContext) -> Action: ...


class TransformHandler:
    node_type = TRANSFORM
    capabilities = (Capability.PURE, Capability.DETERMINISTIC)

    def plan(self, context: PlanContext) -> Action:
        data = apply_transform(
            copy.deepcopy(context.point.data),
            {
                "operations": context.node.config.get("operations", []),
                "create_missing": bool(context.node.config.get("create_missing", False)),
            },
        )
        return Advance(port="out", data=data)


class IfHandler:
    node_type = IF
    capabilities = (Capability.PURE, Capability.DETERMINISTIC)

    def plan(self, context: PlanContext) -> Action:
        config = context.node.config
        selected = evaluate_if(
            context.point.data,
            {
                "path": config["path"],
                "operator": config["operator"],
                "value": config.get("value"),
                "missing": config.get("missing", "error"),
            },
        )
        return Advance(port="true" if selected else "false", data=context.point.data)


class ForkHandler:
    node_type = FORK
    capabilities = (Capability.PURE, Capability.DETERMINISTIC)

    def plan(self, context: PlanContext) -> Action:
        config = context.node.config
        count = int(config["count"])
        join = str(config["join"])
        if count < 2:
            return Fail("INVALID_FORK", "Fork needs at least two branches")
        if not any(node.id == join and node.type == MERGE for node in context.graph.nodes):
            return Fail("INVALID_FORK", "Fork join must reference a merge node")
        return SpawnFork(count=count, join=join, data=context.point.data)


class MergeHandler:
    node_type = MERGE
    capabilities = (Capability.PURE, Capability.DETERMINISTIC)

    def plan(self, context: PlanContext) -> Action:
        if not context.point.fork_stack:
            return Fail("MERGE_WITHOUT_FORK", "Merge reached without an open fork")
        frame = context.point.fork_stack[-1]
        if frame.get("merge_node_id") != context.point.node_id:
            return Fail(
                "MERGE_SCOPE_MISMATCH",
                "Merge does not match the innermost fork scope",
            )
        return ArriveMerge(
            fork_instance_id=str(frame["fork_instance_id"]),
            branch_index=int(frame["branch_index"]),
            data=context.point.data,
        )


class TaskHandler:
    node_type = TASK
    capabilities = (
        Capability.QUEUE,
        Capability.EXTERNAL_IO,
        Capability.WORKSPACE,
    )

    def plan(self, context: PlanContext) -> Action:
        config = context.node.config
        if context.point.task_run_id:
            return self._plan_status(context, config)

        selected = get_path(context.point.data, str(config.get("input_path", "$")))
        if selected is MISSING:
            return Fail("TASK_INPUT_MISSING", "Task input path is missing")
        return EnqueueTask(
            task_id=str(config["task_id"]),
            input_data=selected,
            result_mode=str(config.get("result_mode", "auto")),
            queue_timeout_seconds=config.get("queue_timeout_seconds"),
            execution_timeout_seconds=config.get("execution_timeout_seconds"),
        )

    def _plan_status(
        self, context: PlanContext, config: dict[str, Any]
    ) -> Action:
        status = context.task_status or {}
        state = str(status.get("status") or "")
        if state in TASK_WAITING_STATES:
            return Suspend(
                kind=TASK_WAIT_KIND,
                wake_condition={"after_seconds": TASK_POLL_SECONDS},
            )
        if state in {"cancelling", "cancelled"}:
            return Fail(
                "TASK_CANCELLED",
                str(status.get("safe_error") or "Task was cancelled"),
            )
        if state != "completed":
            message = str(
                status.get("safe_error") or "Task did not complete successfully"
            )
            code = str(status.get("code") or status.get("error_code") or "")
            if not code and message in TASK_NON_RETRYABLE_CODES:
                # Gateways may expose only a safe error string; a bare known
                # code is still an explicit non-retryable outcome.
                code = message
            if code in TASK_NON_RETRYABLE_CODES:
                return Fail(code, message)
            retries = int(config.get("retries", 0) or 0)
            if retries > 0 and context.point.attempt <= retries:
                return Suspend(
                    kind=TASK_RETRY_KIND,
                    wake_condition={"attempt": context.point.attempt + 1},
                )
            return Fail("TASK_FAILED", message)

        result_mode = str(config.get("result_mode", "auto"))
        text = status.get("text")
        parsed: Any = None
        if result_mode == "strict_json":
            try:
                parsed = json.loads(text) if isinstance(text, str) else None
            except ValueError:
                return Fail("TASK_OUTPUT_NOT_JSON", "Task output is not valid JSON")
            if parsed is None:
                return Fail("TASK_OUTPUT_NOT_JSON", "Task output is not valid JSON")
        elif result_mode == "auto" and isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None

        output = {
            "input": context.point.data,
            "task": {
                "task_id": str(config["task_id"]),
                "run_id": context.point.task_run_id,
                "status": "completed",
                "output": {
                    "text": text if isinstance(text, str) else "",
                    "json": parsed,
                },
            },
        }
        return Advance(port="out", data=output)


DEFAULT_HANDLERS: dict[str, NodeHandler] = {
    TRANSFORM: TransformHandler(),
    IF: IfHandler(),
    FORK: ForkHandler(),
    MERGE: MergeHandler(),
    TASK: TaskHandler(),
}

__all__ = [
    "DEFAULT_HANDLERS",
    "ForkHandler",
    "IfHandler",
    "MergeHandler",
    "NodeHandler",
    "PlanContext",
    "PointView",
    "TaskHandler",
    "TransformHandler",
]
