from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict
from typing import Any

import pytest

from opencode_web.vflow.actions import (
    Action,
    Advance,
    ArriveMerge,
    EnqueueTask,
    Fail,
    Finish,
    SpawnFork,
    Suspend,
)
from opencode_web.vflow.handlers import (
    DEFAULT_HANDLERS,
    ForkHandler,
    IfHandler,
    MergeHandler,
    PlanContext,
    PointView,
    TaskHandler,
    TransformHandler,
)
from opencode_web.vflow.models import Graph, Node
from opencode_web.vflow.registry import FORK, IF, MERGE, TASK, TRANSFORM
from opencode_web.vflow.runtime import VFlowRuntime


def make_node(node_id: str, type_id: str, config: dict[str, Any]) -> Node:
    return Node(
        id=node_id,
        type=type_id,
        type_version="1.0",
        config=config,
    )


def make_point(
    data: Any,
    *,
    node_id: str = "node",
    fork_stack: tuple[dict[str, Any], ...] = (),
    attempt: int = 1,
    task_run_id: str | None = None,
) -> PointView:
    return PointView(
        id="point-1",
        run_id="run-1",
        node_id=node_id,
        data=data,
        fork_stack=fork_stack,
        visit_seq=1,
        attempt=attempt,
        task_run_id=task_run_id,
    )


def make_context(
    node: Node,
    data: Any,
    *,
    nodes: tuple[Node, ...] | None = None,
    fork_stack: tuple[dict[str, Any], ...] = (),
    attempt: int = 1,
    task_run_id: str | None = None,
    task_status: dict[str, Any] | None = None,
) -> PlanContext:
    graph = Graph(
        workflow="test",
        entry=node.id,
        nodes=nodes if nodes is not None else (node,),
        edges=(),
    )
    return PlanContext(
        node=node,
        point=make_point(
            data,
            node_id=node.id,
            fork_stack=fork_stack,
            attempt=attempt,
            task_run_id=task_run_id,
        ),
        graph=graph,
        task_status=task_status,
    )


def test_transform_handler_plans_advance_without_mutating_input():
    node = make_node(
        "prepare",
        TRANSFORM,
        {
            "operations": [
                {"op": "set", "path": "$.ok", "value": True},
                {"op": "append", "path": "$.items", "value": 2},
            ]
        },
    )
    context = make_context(node, {"items": [1]})

    action = TransformHandler().plan(context)

    assert action == Advance(port="out", data={"items": [1, 2], "ok": True})
    assert context.point.data == {"items": [1]}


def test_if_handler_selects_port_and_preserves_data():
    node = make_node(
        "decision",
        IF,
        {"path": "$.ok", "operator": "eq", "value": True, "missing": "error"},
    )
    context = make_context(node, {"ok": True})

    accepted = IfHandler().plan(context)

    assert accepted == Advance(port="true", data={"ok": True})
    assert accepted.data is context.point.data

    rejected = IfHandler().plan(make_context(node, {"ok": False}))
    assert rejected == Advance(port="false", data={"ok": False})


def test_action_types_are_frozen_and_json_safe():
    actions: list[Action] = [
        Advance(port="out", data={"value": 1}),
        EnqueueTask(
            task_id="task-1",
            input_data={"payload": True},
            result_mode="strict_json",
            queue_timeout_seconds=10,
            execution_timeout_seconds=60,
        ),
        Suspend(kind="task", wake_condition={"after_seconds": 2}),
        SpawnFork(count=2, join="combined", data={"seed": 1}),
        ArriveMerge(fork_instance_id="fork-1", branch_index=0, data={"side": "left"}),
        Finish(data={"done": True}),
        Fail(code="TASK_FAILED", message="Task did not complete successfully"),
    ]

    for action in actions:
        assert json.loads(json.dumps(asdict(action))) == asdict(action)

    with pytest.raises(FrozenInstanceError):
        actions[0].port = "changed"  # type: ignore[misc]


def test_default_handler_table_matches_builtin_types():
    assert set(DEFAULT_HANDLERS) == {TRANSFORM, IF, FORK, MERGE, TASK}
    assert isinstance(DEFAULT_HANDLERS[TRANSFORM], TransformHandler)
    assert isinstance(DEFAULT_HANDLERS[IF], IfHandler)
    assert isinstance(DEFAULT_HANDLERS[FORK], ForkHandler)
    assert isinstance(DEFAULT_HANDLERS[MERGE], MergeHandler)
    assert isinstance(DEFAULT_HANDLERS[TASK], TaskHandler)
    assert VFlowRuntime(object()).handlers[TASK].node_type == TASK


def test_fork_handler_plans_spawn_and_rejects_invalid_join():
    fork = make_node("split", FORK, {"count": 2, "join": "combined"})
    merge = make_node("combined", MERGE, {})
    context = make_context(fork, {"seed": 1}, nodes=(fork, merge))

    assert ForkHandler().plan(context) == SpawnFork(
        count=2, join="combined", data={"seed": 1}
    )

    no_join = make_node("split", FORK, {"count": 2, "join": "missing"})
    action = ForkHandler().plan(make_context(no_join, {}, nodes=(no_join, merge)))
    assert action == Fail("INVALID_FORK", "Fork join must reference a merge node")

    too_small = make_node("split", FORK, {"count": 1, "join": "combined"})
    action = ForkHandler().plan(make_context(too_small, {}, nodes=(too_small, merge)))
    assert action == Fail("INVALID_FORK", "Fork needs at least two branches")


def test_merge_handler_plans_arrival_and_rejects_scope_errors():
    merge = make_node("combined", MERGE, {})
    frame = {
        "fork_instance_id": "fork-1",
        "fork_node_id": "split",
        "merge_node_id": "combined",
        "branch_index": 1,
        "expected_count": 2,
    }
    context = make_context(
        merge,
        {"side": "right"},
        nodes=(merge,),
        fork_stack=(frame,),
    )

    assert MergeHandler().plan(context) == ArriveMerge(
        fork_instance_id="fork-1",
        branch_index=1,
        data={"side": "right"},
    )
    assert MergeHandler().plan(make_context(merge, {})) == Fail(
        "MERGE_WITHOUT_FORK", "Merge reached without an open fork"
    )

    wrong_scope = {**frame, "merge_node_id": "other"}
    assert MergeHandler().plan(
        make_context(merge, {}, nodes=(merge,), fork_stack=(wrong_scope,))
    ) == Fail("MERGE_SCOPE_MISMATCH", "Merge does not match the innermost fork scope")


def test_task_handler_plans_enqueue_wait_retry_and_completion():
    node = make_node(
        "work",
        TASK,
        {
            "task_id": "task-1",
            "input_path": "$.payload",
            "result_mode": "strict_json",
            "queue_timeout_seconds": 30,
            "execution_timeout_seconds": 120,
            "retries": 2,
        },
    )
    handler = TaskHandler()

    assert handler.plan(make_context(node, {"payload": {"question": "hi"}})) == EnqueueTask(
        task_id="task-1",
        input_data={"question": "hi"},
        result_mode="strict_json",
        queue_timeout_seconds=30,
        execution_timeout_seconds=120,
    )

    waiting = handler.plan(
        make_context(
            node,
            {},
            task_run_id="job-1",
            task_status={"status": "running"},
        )
    )
    assert waiting == Suspend(kind="task", wake_condition={"after_seconds": 2})

    retry = handler.plan(
        make_context(
            node,
            {},
            task_run_id="job-1",
            attempt=1,
            task_status={"status": "failed", "safe_error": "boom"},
        )
    )
    assert retry == Suspend(kind="task_retry", wake_condition={"attempt": 2})

    completed = handler.plan(
        make_context(
            node,
            {"payload": {"question": "hi"}},
            task_run_id="job-1",
            task_status={"status": "completed", "text": '{"answer": 42}'},
        )
    )
    assert completed == Advance(
        port="out",
        data={
            "input": {"payload": {"question": "hi"}},
            "task": {
                "task_id": "task-1",
                "run_id": "job-1",
                "status": "completed",
                "output": {"text": '{"answer": 42}', "json": {"answer": 42}},
            },
        },
    )


def test_task_handler_plans_fail_for_missing_input_and_bad_output():
    node = make_node(
        "work",
        TASK,
        {"task_id": "task-1", "input_path": "$.payload", "result_mode": "strict_json"},
    )
    handler = TaskHandler()

    assert handler.plan(make_context(node, {})) == Fail(
        "TASK_INPUT_MISSING", "Task input path is missing"
    )

    no_retries = make_node("work", TASK, {"task_id": "task-1", "retries": 0})
    assert handler.plan(
        make_context(
            no_retries,
            {},
            task_run_id="job-1",
            task_status={"status": "failed", "safe_error": "boom"},
        )
    ) == Fail("TASK_FAILED", "boom")

    assert handler.plan(
        make_context(
            node,
            {},
            task_run_id="job-1",
            task_status={"status": "completed", "text": "not json"},
        )
    ) == Fail("TASK_OUTPUT_NOT_JSON", "Task output is not valid JSON")
