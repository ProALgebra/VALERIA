from __future__ import annotations

import pytest

from opencode_web.vflow import compile_source, parse
from opencode_web.vflow.models import CompileError, Graph, ParseError
from opencode_web.vflow.registry import FORK, IF, MERGE, TASK, TRANSFORM

SEQUENTIAL = """
workflow "Review" {
  version: 1;
  input: json;
  step context = task.run(task_id: "context.load", input: "$", result_mode: "strict_json");
  step prepare = transform(operations: [
    {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"}
  ]);
  step decision = if(path: "$.context.ok", operator: "eq", value: true, missing: "error");
  step approved = task.run(task_id: "report.approved");
  step rejected = task.run(task_id: "report.rejected");

  start -> context -> prepare -> decision;
  decision.true -> approved -> end;
  decision.false -> rejected -> end;
}
"""

PARALLEL = """
workflow "Parallel" {
  version: 1;
  input: json;
  step split = fork(count: 2, join: combined);
  step a = task.run(task_id: "a");
  step b = task.run(task_id: "b");
  step combined = merge();
  start -> split;
  split[0] -> a -> combined;
  split[1] -> b -> combined;
  combined -> end;
}
"""


def test_parse_workflow_and_comments():
    ast = parse(
        """
        # comment
        workflow "Demo" {
          version: 1;
          input: json;
          step a = task.run(task_id: "a"); // inline
          start -> a -> end
        }
        """
    )
    assert ast.name == "Demo"
    assert ast.version == 1
    assert ast.input_kind == "json"
    assert [step.name for step in ast.steps] == ["a"]
    assert len(ast.edges) == 1


def test_compile_sequential_graph():
    result = compile_source(SEQUENTIAL)
    assert result.graph.workflow == "Review"
    assert result.graph.entry == "context"
    assert [node.id for node in result.graph.nodes] == [
        "context", "prepare", "decision", "approved", "rejected"
    ]
    assert [node.type for node in result.graph.nodes] == [
        TASK, TRANSFORM, IF, TASK, TASK
    ]
    assert result.required_handlers == (
        "core.if@1.0", "core.transform@1.0", "valeria.task@1.0"
    )
    assert result.graph_hash


def test_compile_fork_merge_graph():
    result = compile_source(PARALLEL)
    by_id = {node.id: node for node in result.graph.nodes}
    assert by_id["split"].type == FORK
    assert by_id["combined"].type == MERGE
    assert by_id["split"].config == {"count": 2, "join": "combined"}
    assert [(e.source, e.source_port, e.target) for e in result.graph.edges] == [
        ("start", "out", "split"),
        ("split", "branch[0]", "a"),
        ("a", "out", "combined"),
        ("split", "branch[1]", "b"),
        ("b", "out", "combined"),
        ("combined", "out", "end"),
    ]


def test_graph_json_roundtrip_is_stable():
    graph = compile_source(PARALLEL).graph
    restored = Graph.from_dict(graph.to_dict())
    assert restored == graph
    assert restored.canonical_json() == graph.canonical_json()


def test_unknown_node_type_is_reported():
    with pytest.raises(CompileError) as error:
        compile_source(
            """
            workflow "Bad" {
              version: 1;
              input: json;
              step x = python.run(code: "print(1)");
              start -> x -> end;
            }
            """
        )
    assert error.value.diagnostics[0].code == "COMPILE_UNKNOWN_NODE_TYPE"


def test_if_requires_both_ports():
    with pytest.raises(CompileError) as error:
        compile_source(
            """
            workflow "Bad" {
              version: 1;
              input: json;
              step decision = if(path: "$.ok", operator: "eq", value: true);
              step yes = task.run(task_id: "yes");
              start -> decision;
              decision.true -> yes -> end;
            }
            """
        )
    codes = {item.code for item in error.value.diagnostics}
    assert "COMPILE_IF_PORT_COUNT" in codes


def test_task_fan_out_is_rejected():
    with pytest.raises(CompileError) as error:
        compile_source(
            """
            workflow "Bad" {
              version: 1;
              input: json;
              step a = task.run(task_id: "a");
              step b = task.run(task_id: "b");
              step c = task.run(task_id: "c");
              start -> a;
              a -> b;
              a -> c;
              b -> end;
              c -> end;
            }
            """
        )
    codes = {item.code for item in error.value.diagnostics}
    assert "COMPILE_FAN_OUT" in codes


def test_fork_requires_merge():
    with pytest.raises(CompileError) as error:
        compile_source(
            """
            workflow "Bad" {
              version: 1;
              input: json;
              step split = fork(count: 2, join: not_merge);
              step a = task.run(task_id: "a");
              step not_merge = task.run(task_id: "merge");
              start -> split;
              split[0] -> a -> end;
              split[1] -> not_merge -> end;
            }
            """
        )
    codes = {item.code for item in error.value.diagnostics}
    assert "COMPILE_FORK_JOIN_NOT_MERGE" in codes


def test_parse_error_has_position():
    with pytest.raises(ParseError) as error:
        parse('workflow "Bad" { version: 1; input: json; step x = task.run(; }')
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.line is not None
    assert diagnostic.column is not None
