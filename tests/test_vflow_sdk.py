from __future__ import annotations

import json

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database
from opencode_web.vflow import compile_graph, compile_source
from opencode_web.vflow.actions import Advance, Fail
from opencode_web.vflow.blocks.example import (
    ECHO,
    ECHO_SPEC,
    EchoHandler,
    build_example_handlers,
    build_example_registry,
    register_example_block,
)
from opencode_web.vflow.capabilities import Capability
from opencode_web.vflow.handlers import DEFAULT_HANDLERS, PlanContext, PointView
from opencode_web.vflow.models import Graph, Node
from opencode_web.vflow.registry import NodeRegistry, NodeSpec
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.sdk import register_block
from opencode_web.vflow.service import VFlowService

ECHO_SOURCE = """\
workflow "sdk-echo" {
  version: 1;
  input: json;
  step copy = custom.echo(tag: "sdk");
  start -> copy -> end
}
"""


def make_spec(**overrides) -> NodeSpec:
    values = {
        "type": "custom.test",
        "version": "1.0",
        "title": "Test",
        "category": "custom",
        "description": "Capability model test block",
        "inputs": ("in",),
        "outputs": ("out",),
        "call_names": ("custom.test",),
        "capabilities": (Capability.PURE, Capability.DETERMINISTIC),
    }
    values.update(overrides)
    return NodeSpec(**values)


def make_context(data, *, config=None, node_id="copy") -> PlanContext:
    node = Node(
        id=node_id,
        type=ECHO,
        type_version="1.0",
        config=dict(config or {}),
    )
    graph = Graph(workflow="sdk", entry=node.id, nodes=(node,), edges=())
    return PlanContext(
        node=node,
        point=PointView(
            id="point-1",
            run_id="run-1",
            node_id=node.id,
            data=data,
            fork_stack=(),
            visit_seq=1,
            attempt=1,
            task_run_id=None,
        ),
        graph=graph,
    )


def test_capability_model_validates_declarations():
    spec = make_spec(capabilities=("pure", "deterministic"))
    assert spec.capabilities == ("pure", "deterministic")

    with pytest.raises(ValueError, match="Unknown VFlow capability"):
        make_spec(capabilities=("eval",))
    with pytest.raises(ValueError, match="Duplicate VFlow capability"):
        make_spec(capabilities=("pure", "pure"))
    with pytest.raises(ValueError, match="'pure' cannot be combined"):
        make_spec(capabilities=("pure", "external_io"))


def test_register_block_validates_handler_binding_atomically():
    registry = NodeRegistry()
    handlers = dict(DEFAULT_HANDLERS)

    class WrongTypeHandler(EchoHandler):
        node_type = "custom.other"

    with pytest.raises(ValueError, match="does not match NodeSpec type"):
        register_block(registry, handlers, ECHO_SPEC, WrongTypeHandler())
    assert registry.get(ECHO, "1.0") is None
    assert ECHO not in handlers

    class DriftedCapabilitiesHandler(EchoHandler):
        capabilities = ("external_io", "secrets")

    with pytest.raises(ValueError, match="capabilities do not match"):
        register_block(registry, handlers, ECHO_SPEC, DriftedCapabilitiesHandler())
    assert registry.get(ECHO, "1.0") is None
    assert ECHO not in handlers


def test_runtime_rejects_handler_capability_drift():
    class DriftedHandler(EchoHandler):
        capabilities = ("external_io", "secrets")

    with pytest.raises(ValueError, match="capabilities do not match"):
        VFlowRuntime(
            object(),
            registry=build_example_registry(),
            handlers={**build_example_handlers(), ECHO: DriftedHandler()},
        )


def test_custom_registry_registration_and_call_form_resolution():
    registry = NodeRegistry()
    handlers = dict(DEFAULT_HANDLERS)

    registered = register_example_block(registry, handlers)

    assert registered is ECHO_SPEC
    assert registry.get(ECHO, "1.0") is ECHO_SPEC
    assert registry.resolve_call("custom.echo") is ECHO_SPEC
    assert registry.resolve_call("echo") is ECHO_SPEC
    assert handlers[ECHO].node_type == ECHO
    assert handlers[ECHO].capabilities == ECHO_SPEC.capabilities

    with pytest.raises(ValueError, match="already registered"):
        register_example_block(registry, handlers)


def test_compile_source_and_graph_use_example_call_forms():
    registry = build_example_registry()

    result = compile_source(ECHO_SOURCE, registry=registry)
    node = result.graph.nodes[0]
    assert node.type == ECHO
    assert node.type_version == "1.0"
    assert node.config == {"tag": "sdk"}
    assert result.required_handlers == ("custom.echo@1.0",)

    alias = compile_source(ECHO_SOURCE.replace("custom.echo", "echo"), registry=registry)
    assert alias.graph.nodes[0].type == ECHO

    graph = {
        "ir_version": 1,
        "workflow": "sdk-graph",
        "entry": "copy",
        "nodes": [
            {
                "id": "copy",
                "type": ECHO,
                "type_version": "1.0",
                "config": {"tag": "graph"},
                "ui": {},
            }
        ],
        "edges": [
            {"source": "start", "source_port": "out", "target": "copy"},
            {"source": "copy", "source_port": "out", "target": "end"},
        ],
    }
    compiled = compile_graph(graph, registry=registry)
    assert compiled.graph.nodes[0].config == {"tag": "graph"}
    assert compiled.required_handlers == ("custom.echo@1.0",)


def test_echo_handler_plans_pass_through_and_tagged_advance():
    handler = EchoHandler()
    source = {"seed": [1, 2]}

    action = handler.plan(make_context(source))
    assert action == Advance(port="out", data={"seed": [1, 2]})
    assert action.data is not source

    tagged = handler.plan(make_context(source, config={"tag": "sdk"}))
    assert tagged == Advance(port="out", data={"tag": "sdk", "value": {"seed": [1, 2]}})
    assert source == {"seed": [1, 2]}

    invalid = handler.plan(make_context(source, config={"tag": 7}))
    assert invalid == Fail(
        "ECHO_INVALID_TAG",
        "Echo tag must be a string of at most 200 characters",
    )


def test_manifest_exposes_schema_driven_ui_contract():
    manifest = ECHO_SPEC.to_dict()

    assert {
        "type",
        "version",
        "title",
        "category",
        "description",
        "inputs",
        "outputs",
        "config_schema",
        "capabilities",
        "call_names",
    } <= set(manifest)
    assert manifest["type"] == ECHO
    assert manifest["version"] == "1.0"
    assert manifest["category"] == "custom"
    assert manifest["config_schema"]["properties"]["tag"]["type"] == "string"
    assert manifest["call_names"] == ["custom.echo", "echo"]
    assert manifest["capabilities"] == ["pure", "deterministic"]

    service = VFlowService(object(), registry=build_example_registry())
    entries = {item["type"]: item for item in service.block_registry()}
    assert entries[ECHO]["capabilities"] == ["pure", "deterministic"]
    assert entries[ECHO]["config_schema"] == ECHO_SPEC.config_schema


async def test_block_registry_api_exposes_example_without_ui_changes(
    settings, monkeypatch
):
    import opencode_web.vflow.api as vflow_api

    registry = build_example_registry()
    original_service = vflow_api.VFlowService

    def service(db, **kwargs):
        return original_service(db, registry=registry, **kwargs)

    monkeypatch.setattr(vflow_api, "VFlowService", service)

    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={
                "login": "admin",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        response = await client.get("/api/admin/vflow/block-registry")
        assert response.status_code == 200, response.text
        items = {item["type"]: item for item in response.json()}
        assert items[ECHO]["version"] == "1.0"
        assert items[ECHO]["title"] == "Echo (example)"
        assert items[ECHO]["category"] == "custom"
        assert items[ECHO]["config_schema"]["properties"]["tag"]["type"] == "string"
        assert items[ECHO]["call_names"] == ["custom.echo", "echo"]
        assert items[ECHO]["capabilities"] == ["pure", "deterministic"]


def test_demo_runtime_executes_example_block(tmp_path):
    database = Database(tmp_path / "sdk.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    try:
        registry = build_example_registry()
        handlers = build_example_handlers()
        service = VFlowService(database, registry=registry)
        definition = service.create_definition(
            admin["id"], slug="sdk-echo", title="SDK echo"
        )
        service.save_draft(definition["id"], admin["id"], source=ECHO_SOURCE)
        service.publish(definition["id"], admin["id"])
        version = service.repository.get_published(definition["id"])
        assert version is not None

        runtime = VFlowRuntime(database, registry=registry, handlers=handlers)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"seed": 1},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded", finished
        assert json.loads(finished["final_result_json"]) == {
            "tag": "sdk",
            "value": {"seed": 1},
        }
    finally:
        database.close()
