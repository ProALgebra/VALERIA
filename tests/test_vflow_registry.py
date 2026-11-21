from __future__ import annotations

import pytest

from opencode_web.vflow import compile_source
from opencode_web.vflow.models import CompileError
from opencode_web.vflow.registry import (
    DEFAULT_REGISTRY,
    FORK,
    IF,
    MERGE,
    TASK,
    TRANSFORM,
    NodeRegistry,
    NodeSpec,
)

CUSTOM_CALL_SOURCE = """\
workflow "custom-call" {
  version: 1;
  input: json;
  step wait = custom.delay(seconds: 2);
  start -> wait -> end
}
"""

ALIAS_SOURCE = """\
workflow "alias" {
  version: 1;
  input: json;
  step prepare = custom.transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> prepare -> end
}
"""

UNKNOWN_CALL_SOURCE = """\
workflow "unknown" {
  version: 1;
  input: json;
  step hack = python.run(code: "print(1)");
  start -> hack -> end
}
"""


def custom_delay_spec(**overrides) -> NodeSpec:
    values = {
        "type": "custom.delay",
        "version": "1.0",
        "title": "Delay",
        "category": "custom",
        "description": "Code-only delay block",
        "inputs": ("in",),
        "outputs": ("out",),
        "call_names": ("custom.delay", "delay"),
    }
    values.update(overrides)
    return NodeSpec(**values)


def test_builtin_call_forms_resolve_through_registry():
    expected = {
        "task.run": TASK,
        "transform": TRANSFORM,
        "if": IF,
        "fork": FORK,
        "merge": MERGE,
    }
    for call_name, type_id in expected.items():
        spec = DEFAULT_REGISTRY.resolve_call(call_name)
        assert spec is not None
        assert spec.type == type_id
    assert DEFAULT_REGISTRY.resolve_call("python.run") is None


def test_additional_call_form_for_existing_type_compiles_without_parser_change():
    registry = NodeRegistry()
    updated = registry.register_call_form("custom.transform", TRANSFORM)

    assert updated.type == TRANSFORM
    assert registry.resolve_call("custom.transform").type == TRANSFORM
    assert set(registry.get(TRANSFORM).call_names) == {"transform", "custom.transform"}

    result = compile_source(ALIAS_SOURCE, registry=registry)

    node = result.graph.nodes[0]
    assert node.type == TRANSFORM
    assert node.config == {
        "operations": [{"op": "set", "path": "$.ok", "value": True}],
        "create_missing": False,
    }
    # The original call form keeps working after adding an alias.
    assert compile_source(
        ALIAS_SOURCE.replace("custom.transform", "transform"), registry=registry
    ).graph.nodes[0].type == TRANSFORM


def test_custom_type_call_names_are_registry_metadata():
    registry = NodeRegistry()
    registry.register(custom_delay_spec())

    assert registry.resolve_call("delay").type == "custom.delay"
    assert registry.resolve_call("custom.delay").type == "custom.delay"

    result = compile_source(CUSTOM_CALL_SOURCE, registry=registry)

    node = result.graph.nodes[0]
    assert node.id == "wait"
    assert node.type == "custom.delay"
    assert node.type_version == "1.0"
    assert node.config == {"seconds": 2}
    assert result.required_handlers == ("custom.delay@1.0",)


def test_register_call_form_accepts_unregistered_spec():
    registry = NodeRegistry()
    spec = custom_delay_spec(call_names=())
    updated = registry.register_call_form("delay", spec)

    assert updated is registry.get("custom.delay")
    assert registry.get("custom.delay").call_names == ("delay",)
    assert registry.resolve_call("delay").type == "custom.delay"


def test_duplicate_call_name_registration_is_rejected_atomically():
    registry = NodeRegistry()

    with pytest.raises(ValueError, match="call form already registered"):
        registry.register(custom_delay_spec(call_names=("transform",)))
    assert registry.get("custom.delay") is None
    assert registry.resolve_call("transform").type == TRANSFORM

    with pytest.raises(ValueError, match="call form already registered"):
        registry.register_call_form("fork", "custom.delay")
    assert registry.resolve_call("fork").type == FORK


def test_duplicate_call_names_inside_one_spec_are_rejected():
    registry = NodeRegistry()
    with pytest.raises(ValueError, match="duplicates"):
        registry.register(custom_delay_spec(call_names=("delay", "delay")))
    assert registry.get("custom.delay") is None


def test_unknown_call_form_is_a_validation_error():
    with pytest.raises(CompileError) as error:
        compile_source(UNKNOWN_CALL_SOURCE, registry=NodeRegistry())
    assert error.value.diagnostics[0].code == "COMPILE_UNKNOWN_NODE_TYPE"

    registry = NodeRegistry()
    registry.register(custom_delay_spec())
    with pytest.raises(CompileError) as missing_alias:
        compile_source(ALIAS_SOURCE, registry=registry)
    assert missing_alias.value.diagnostics[0].code == "COMPILE_UNKNOWN_NODE_TYPE"
