from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from opencode_web.database import Database, utcnow
from opencode_web.json_processor.integration import (
    JsonProcessorVFlowAdapter,
    build_worker,
    resolve_env_api_key_ref,
)
from opencode_web.json_processor.repository import JsonProcessorRepository
from opencode_web.vflow.compiler import compile_source
from opencode_web.vflow.gateway import ValeriaTaskGateway
from opencode_web.vflow.models import CompileError
from opencode_web.vflow.registry import DEFAULT_REGISTRY, TASK
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}

VALID_SOURCE = """\
workflow "valid-json" {{
  version: 1;
  input: json;
  step work = task.run(
    task_id: "{task_id}",
    output_mode: "json",
    output_schema: {schema}
  );
  step decide = if(path: "$.output.json.ok", operator: "eq", value: true);
  step yes = transform(operations: [{{op: "set", path: "$.accepted", value: true}}]);
  step no = transform(operations: [{{op: "set", path: "$.accepted", value: false}}]);
  start -> work -> decide;
  decide.true -> yes -> end;
  decide.false -> no -> end
}}
"""

REPAIR_SOURCE = """\
workflow "repair-json" {{
  version: 1;
  input: json;
  step work = task.run(
    task_id: "{task_id}",
    output_mode: "json",
    output_schema: {schema},
    on_json_error: "repair",
    repair_trigger: "{trigger}"
  );
  step after = transform(operations: [{{op: "set", path: "$.after", value: true}}]);
  start -> work -> after -> end
}}
"""

FORK_SOURCE = """\
workflow "fork-json" {{
  version: 1;
  input: json;
  step split = fork(count: 2, join: combined);
  step left = task.run(
    task_id: "{left}",
    output_mode: "json",
    on_json_error: "repair"
  );
  step right = task.run(
    task_id: "{right}",
    output_mode: "json",
    on_json_error: "repair"
  );
  step combined = merge();
  start -> split;
  split[0] -> left -> combined;
  split[1] -> right -> combined;
  combined -> end
}}
"""


def make_env(tmp_path):
    database = Database(tmp_path / "json-processor-integration.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    return database, admin, VFlowService(database)


def create_task(database, admin, title="Echo"):
    return database.create_task(
        admin_id=admin["id"],
        title=title,
        description="",
        content="Return JSON",
        agent="build",
        provider_id="openai",
        model_id="gpt-4",
        model_variant=None,
        published=True,
    )


def publish(service, admin, slug, source):
    definition = service.create_definition(admin["id"], slug=slug, title=slug)
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def enable_local_processor(database):
    repository = JsonProcessorRepository(database)
    repository.update_settings(
        {
            "enabled": True,
            "local_repair_enabled": True,
            "max_llm_attempts": 0,
        },
        expected_version=1,
    )
    return repository


def force_due(database):
    with database.transaction() as connection:
        connection.execute(
            """UPDATE vflow_points SET next_check_at = ?
               WHERE status IN ('waiting_task', 'processing_output')""",
            (utcnow(),),
        )


def run_to_completion(runtime, run_id, database, rounds=6):
    run = runtime.get_run(run_id)
    for _ in range(rounds):
        runtime.run_until_idle(run_id)
        run = runtime.get_run(run_id)
        if run["status"] != "running":
            break
        force_due(database)
    return run


def process_worker(database):
    _repository, _service, worker = build_worker(database)
    return asyncio.run(worker.run_once())


def point_row(database, run_id):
    return database._one("SELECT * FROM vflow_points WHERE run_id = ?", (run_id,))


def jobs(database, run_id):
    return database._all(
        "SELECT * FROM json_repair_jobs WHERE vflow_run_id = ? ORDER BY created_at, id",
        (run_id,),
    )


class FakeTaskGateway:
    def __init__(self, text: str):
        self.text = text
        self.enqueued: list[dict] = []
        self.cancelled: list[str] = []
        self._next = 0

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        self._next += 1
        return f"job-{self._next}"

    def status(self, task_run_id: str):
        return {"status": "completed", "text": self.text}

    def cancel(self, task_run_id: str) -> None:
        self.cancelled.append(task_run_id)


def test_task_compiler_validates_new_fields_and_pins_them():
    source = REPAIR_SOURCE.format(
        task_id="task-1",
        schema=json.dumps(SCHEMA),
        trigger="invalid_json_or_schema",
    )
    compiled = compile_source(source)
    node = next(item for item in compiled.graph.nodes if item.type == TASK)
    assert node.config["output_mode"] == "json"
    assert node.config["output_schema"] == SCHEMA
    assert node.config["on_json_error"] == "repair"
    assert node.config["repair_trigger"] == "invalid_json_or_schema"

    spec = DEFAULT_REGISTRY.get(TASK)
    assert spec.config_schema["properties"]["output_mode"]["default"] == "text"
    assert spec.config_schema["properties"]["on_json_error"]["enum"] == [
        "fail",
        "repair",
    ]
    assert (
        spec.config_schema["properties"]["repair_trigger"]["enum"]
        == ["invalid_json", "invalid_json_or_schema"]
    )


def test_task_compiler_rejects_invalid_combinations():
    base = """\
workflow "bad" {{
  version: 1;
  input: json;
  step work = task.run(task_id: "task-1"{extra});
  start -> work -> end
}}
"""
    with pytest.raises(CompileError) as invalid_schema:
        compile_source(
            base.format(
                extra=', output_mode: "json", output_schema: {"type": "wat"}'
            )
        )
    assert "COMPILE_INVALID_JSON_SCHEMA" in {
        item.code for item in invalid_schema.value.diagnostics
    }

    with pytest.raises(CompileError) as trigger_without_repair:
        compile_source(base.format(extra=', repair_trigger: "invalid_json"'))
    assert "COMPILE_INVALID_REPAIR_TRIGGER" in {
        item.code for item in trigger_without_repair.value.diagnostics
    }

    with pytest.raises(CompileError) as both_engines:
        compile_source(
            base.format(
                extra=(
                    ', output_mode: "json", on_json_error: "repair",'
                    " max_json_repairs: 1"
                )
            )
        )
    assert "COMPILE_REPAIR_ENGINE_CONFLICT" in {
        item.code for item in both_engines.value.diagnostics
    }

    with pytest.raises(CompileError) as repair_without_json:
        compile_source(
            base.format(extra=', on_json_error: "repair"')
        )
    assert "COMPILE_INVALID_OUTPUT_MODE" in {
        item.code for item in repair_without_json.value.diagnostics
    }

    # Legacy pipelines keep compiling unchanged.
    legacy = compile_source(
        base.format(
            extra=(
                ', result_mode: "strict_json", json_schema: '
                + json.dumps(SCHEMA)
                + ", max_json_repairs: 1"
            )
        )
    )
    node = next(item for item in legacy.graph.nodes if item.type == TASK)
    assert node.config["output_mode"] == "text"
    assert node.config["max_json_repairs"] == 1


def test_task_entity_persists_and_validates_output_configuration(tmp_path):
    database, admin, _service = make_env(tmp_path)
    try:
        task = database.create_task(
            admin_id=admin["id"],
            title="JSON task",
            description="",
            content="Return JSON",
            agent="build",
            provider_id="openai",
            model_id="gpt-4",
            model_variant=None,
            published=True,
            output_mode="json",
            output_schema=SCHEMA,
            on_json_error="repair",
            repair_trigger="invalid_json_or_schema",
        )
        assert task["output_mode"] == "json"
        assert json.loads(task["output_schema_json"]) == SCHEMA
        assert task["on_json_error"] == "repair"
        assert task["repair_trigger"] == "invalid_json_or_schema"

        updated = database.update_task(task["id"], {"output_schema": None})
        assert updated["output_schema_json"] is None
        assert updated["output_mode"] == "json"

        with pytest.raises(ValueError):
            database.update_task(task["id"], {"output_schema": {"type": "wat"}})
        with pytest.raises(ValueError):
            database.create_task(
                admin_id=admin["id"],
                title="Bad",
                description="",
                content="Return JSON",
                agent="build",
                provider_id=None,
                model_id=None,
                model_variant=None,
                published=True,
                output_mode="text",
                on_json_error="repair",
            )
    finally:
        database.close()


def test_valid_json_uses_output_contract_without_processor_job(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service,
            admin,
            "valid-json",
            VALID_SOURCE.format(task_id=task["id"], schema=json.dumps(SCHEMA)),
        )
        runtime = VFlowRuntime(database, FakeTaskGateway('{"ok": true}'))
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"seed": 1},
        )
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "succeeded", finished
        result = json.loads(finished["final_result_json"])
        assert result["accepted"] is True
        assert result["output"]["json"] == {"ok": True}
        assert result["task"]["output"]["json"] == {"ok": True}
        assert result["task"]["output"]["text"] == '{"ok": true}'
        assert result["input"] == {"seed": 1}
        assert jobs(database, run["id"]) == []

        contract = result
        assert contract["task"]["output"]["json"] == {"ok": True}
        assert contract["task"]["output"]["text"] == '{"ok": true}'
    finally:
        database.close()


def test_invalid_json_repair_job_advances_on_success(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "repair-json",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        gateway = ValeriaTaskGateway(database)
        runtime = VFlowRuntime(
            database,
            gateway,
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"seed": 1},
        )
        runtime.run_until_idle(run["id"])
        original = point_row(database, run["id"])
        database.complete_task_run(
            original["task_run_id"],
            {
                "message_id": "m-task",
                "text": '```json\n{"ok": true}\n```',
            },
        )
        force_due(database)
        runtime.run_until_idle(run["id"])
        point = point_row(database, run["id"])
        assert point["status"] == "processing_output", point
        assert point["json_processor_job_id"]
        created = jobs(database, run["id"])
        assert len(created) == 1
        job = created[0]
        assert job["source_type"] == "vflow"
        assert job["vflow_run_id"] == run["id"]
        assert job["point_id"] == point["id"]
        assert job["task_run_id"] == original["task_run_id"]
        assert job["status"] == "queued"

        stats = process_worker(database)
        assert stats.succeeded == 1, stats
        assert JsonProcessorRepository(database).get_job(job["id"])["status"] == "succeeded"

        force_due(database)
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "succeeded", finished
        result = json.loads(finished["final_result_json"])
        assert result["after"] is True
        assert result["task"]["output"]["json"] == {"ok": True}
        assert result["output"]["json"] == {"ok": True}
        assert result["task"]["run_id"] == original["task_run_id"]
        assert result["task"]["task_id"] == task["id"]
        assert result["task"]["status"] == "completed"
    finally:
        database.close()


def test_processor_failure_fails_run_without_downstream(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "repair-failure",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            ValeriaTaskGateway(database),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        point = point_row(database, run["id"])
        database.complete_task_run(
            point["task_run_id"], {"message_id": "m-bad", "text": "not json"}
        )
        force_due(database)
        runtime.run_until_idle(run["id"])
        assert point_row(database, run["id"])["status"] == "processing_output"
        created = jobs(database, run["id"])
        assert len(created) == 1

        stats = process_worker(database)
        assert stats.failed == 1, stats
        assert JsonProcessorRepository(database).get_job(created[0]["id"])["status"] == "failed"

        force_due(database)
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "OUTPUT_INVALID"
        assert "not json" not in (finished["safe_error_message"] or "")
        assert point_row(database, run["id"])["status"] == "cancelled"
    finally:
        database.close()


def test_processor_disabled_fails_explicitly(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service,
            admin,
            "repair-disabled",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "JSON_PROCESSOR_NOT_CONFIGURED"
        assert jobs(database, run["id"]) == []
    finally:
        database.close()


def test_repair_trigger_invalid_json_does_not_repair_schema_mismatch(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "schema-trigger",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway('{"ok": "wrong"}'),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "OUTPUT_INVALID"
        assert jobs(database, run["id"]) == []
    finally:
        database.close()


def test_repair_trigger_schema_mismatch_creates_job(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "schema-trigger-repair",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json_or_schema",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway('{"ok": "wrong"}'),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        point = point_row(database, run["id"])
        assert point["status"] == "processing_output", point
        assert len(jobs(database, run["id"])) == 1
    finally:
        database.close()


def test_repeated_completion_is_idempotent(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "repair-idempotent",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        point = point_row(database, run["id"])
        first_job_id = point["json_processor_job_id"]
        assert first_job_id
        assert len(jobs(database, run["id"])) == 1

        # Simulate a crash after the job insert but before the point update was
        # committed: the Task is completed again and the point has no job id.
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'waiting_task', json_processor_job_id = NULL,
                       next_check_at = ?
                   WHERE id = ?""",
                (utcnow(), point["id"]),
            )
        runtime.run_until_idle(run["id"])
        point = point_row(database, run["id"])
        assert point["json_processor_job_id"] == first_job_id
        assert len(jobs(database, run["id"])) == 1
        assert database._one(
            "SELECT COUNT(*) AS count FROM json_repair_jobs"
        )["count"] == 1
    finally:
        database.close()


def test_cancel_run_cancels_pending_processor_job(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "repair-cancel",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        job = jobs(database, run["id"])[0]
        assert job["status"] == "queued"
        assert runtime.cancel_run(run["id"]) is True
        assert JsonProcessorRepository(database).get_job(job["id"])["status"] == "cancelled"
    finally:
        database.close()


def test_processor_wait_respects_run_timeout_and_cancels_job(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "repair-timeout",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        job = jobs(database, run["id"])[0]
        assert job["status"] == "queued"

        with database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_runs SET started_at = '2000-01-01T00:00:00+00:00' "
                "WHERE id = ?",
                (run["id"],),
            )
        force_due(database)
        runtime.run_until_idle(run["id"])
        finished = runtime.get_run(run["id"])
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "LIMIT_RUN_SECONDS"
        assert JsonProcessorRepository(database).get_job(job["id"])["status"] == "cancelled"
    finally:
        database.close()


def test_fork_branches_create_independent_processor_jobs(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        left = create_task(database, admin, "Left")
        right = create_task(database, admin, "Right")
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "fork-repair",
            FORK_SOURCE.format(left=left["id"], right=right["id"]),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"seed": 1},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        created = jobs(database, run["id"])
        assert len(created) == 2
        assert len({job["point_id"] for job in created}) == 2
        assert {job["status"] for job in created} == {"queued"}
        points = database._all(
            """SELECT * FROM vflow_points WHERE run_id = ? AND status = 'processing_output'""",
            (run["id"],),
        )
        assert len(points) == 2
    finally:
        database.close()


def test_env_api_key_ref_resolution_never_returns_reference():
    assert resolve_env_api_key_ref("env:MY_KEY", {"MY_KEY": "secret"}) == "secret"
    assert resolve_env_api_key_ref("MY_KEY", {"MY_KEY": "secret"}) == "secret"
    assert resolve_env_api_key_ref("MISSING", {}) is None
    assert resolve_env_api_key_ref("bad ref!", {"bad ref!": "secret"}) is None


async def test_app_lifespan_starts_worker_only_when_enabled(settings, tmp_path):
    from opencode_web.app import create_app

    database = Database(settings.database_path, settings.session_secret)
    database.initialize()
    enable_local_processor(database)
    database.close()

    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        worker = app.state.json_processor_worker
        assert worker is not None
        assert worker.is_running is True
        assert app.state.json_processor_vflow is not None
        assert app.state.vflow_runtime.json_processor is app.state.json_processor_vflow
        assert callable(app.state.json_processor_api_key_resolver)
    assert worker.is_running is False


async def test_app_lifespan_does_not_start_disabled_worker(settings):
    from opencode_web.app import create_app

    app = create_app(settings, start_dispatcher=True)
    async with app.router.lifespan_context(app):
        assert app.state.json_processor_worker is None
        assert app.state.json_processor_vflow is None


async def test_admin_task_api_exposes_output_fields(settings):
    import httpx

    from opencode_web.app import create_app

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
        client.headers["x-csrf-token"] = login.json()["csrf_token"]
        created = await client.post(
            "/api/admin/tasks",
            json={
                "title": "JSON task",
                "description": "structured",
                "content": "Return JSON",
                "agent": "build",
                "model": "provider/coder",
                "is_published": True,
                "output_mode": "json",
                "output_schema": SCHEMA,
                "on_json_error": "repair",
                "repair_trigger": "invalid_json_or_schema",
            },
        )
        assert created.status_code == 201, created.text
        task = created.json()
        assert task["output_mode"] == "json"
        assert task["output_schema"] == SCHEMA
        assert task["on_json_error"] == "repair"
        assert task["repair_trigger"] == "invalid_json_or_schema"

        patched = await client.patch(
            f"/api/admin/tasks/{task['id']}",
            json={"output_schema": None, "on_json_error": "fail"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["output_schema"] is None
        assert patched.json()["on_json_error"] == "fail"

        invalid = await client.patch(
            f"/api/admin/tasks/{task['id']}",
            json={"output_schema": {"type": "wat"}},
        )
        assert invalid.status_code == 422, invalid.text




def test_fork_branch_invalid_output_creates_only_that_repair_job(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        left = create_task(database, admin, "Left")
        right = create_task(database, admin, "Right")
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "fork-mixed-json",
            FORK_SOURCE.format(left=left["id"], right=right["id"]),
        )
        runtime = VFlowRuntime(
            database,
            ValeriaTaskGateway(database),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"seed": 1},
        )
        runtime.run_until_idle(run["id"])
        all_points = database._all(
            "SELECT * FROM vflow_points WHERE run_id = ? ORDER BY node_id",
            (run["id"],),
        )
        points = [
            item for item in all_points if item["node_id"] in {"left", "right"}
        ]
        assert {item["node_id"] for item in points} == {"left", "right"}
        for point in points:
            text = '{"ok": true}' if point["node_id"] == "right" else "not json"
            database.complete_task_run(
                point["task_run_id"],
                {"message_id": f"m-{point['node_id']}", "text": text},
            )

        force_due(database)
        runtime.run_until_idle(run["id"])
        created = jobs(database, run["id"])
        assert len(created) == 1
        job = created[0]
        left_point = next(item for item in points if item["node_id"] == "left")
        right_point = next(item for item in points if item["node_id"] == "right")
        assert job["point_id"] == left_point["id"]
        assert job["source_type"] == "vflow"
        assert point_row(database, run["id"]) is not None
        refreshed_left = database._one(
            "SELECT * FROM vflow_points WHERE id = ?", (left_point["id"],)
        )
        refreshed_right = database._one(
            "SELECT * FROM vflow_points WHERE id = ?", (right_point["id"],)
        )
        assert refreshed_left["status"] == "processing_output"
        assert refreshed_left["json_processor_job_id"] == job["id"]
        assert refreshed_right["status"] != "processing_output"
        assert refreshed_right["json_processor_job_id"] is None

        stats = process_worker(database)
        assert stats.failed == 1, stats
        assert JsonProcessorRepository(database).get_job(job["id"])["status"] == "failed"

        force_due(database)
        finished = run_to_completion(runtime, run["id"], database)
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "OUTPUT_INVALID"
        assert len(jobs(database, run["id"])) == 1
    finally:
        database.close()


def test_vflow_run_cancellation_is_seen_before_processing(tmp_path):
    database, admin, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        enable_local_processor(database)
        definition, version = publish(
            service,
            admin,
            "cancel-before-process",
            REPAIR_SOURCE.format(
                task_id=task["id"],
                schema=json.dumps(SCHEMA),
                trigger="invalid_json",
            ),
        )
        runtime = VFlowRuntime(
            database,
            FakeTaskGateway("not json"),
            json_processor=JsonProcessorVFlowAdapter(
                JsonProcessorRepository(database)
            ),
        )
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])
        job = jobs(database, run["id"])[0]
        assert job["status"] == "queued"

        # Simulate an operator/run-level cancellation that committed the run
        # state but did not reach the processor row (crash window).
        with database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_runs SET status = 'cancelled' WHERE id = ?",
                (run["id"],),
            )

        stats = process_worker(database)
        assert stats.cancelled == 1, stats
        assert stats.failed == 0, stats
        stored = JsonProcessorRepository(database).get_job(job["id"])
        assert stored["status"] == "cancelled"
        assert stored["error_code"] == "CANCELLED"
        assert stored["result_json"] is None
    finally:
        database.close()


def test_vflow_processor_jobs_respect_retention_metadata(tmp_path):
    database, admin, _service = make_env(tmp_path)
    try:
        enable_local_processor(database)
        repository = JsonProcessorRepository(database)
        adapter = JsonProcessorVFlowAdapter(repository)
        job = adapter.create_repair_job(
            raw_output="not json",
            raw_output_ref="vflow://run-retention/point-retention",
            schema=SCHEMA,
            task_run_id=None,
            vflow_run_id="run-retention",
            point_id="point-retention",
        )
        repository.claim_due(1, lease_seconds=60, owner="retention-worker")
        repository.complete_job(
            job["id"],
            lease_owner="retention-worker",
            result_json={"ok": True},
            method="local",
        )
        attempt = repository.add_attempt(job["id"], method="local")
        now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE json_repair_jobs SET finished_at = ? WHERE id = ?",
                ((now - timedelta(days=40)).isoformat(), job["id"]),
            )
            connection.execute(
                "UPDATE json_repair_attempts SET created_at = ? WHERE id = ?",
                ((now - timedelta(days=100)).isoformat(), attempt["id"]),
            )

        result = repository.retention_purge(now=now)

        assert result["jobs_output_purged"] == 1
        assert result["attempts_deleted"] == 1
        stored = repository.get_job(job["id"])
        assert stored is not None
        assert stored["source_type"] == "vflow"
        assert stored["vflow_run_id"] == "run-retention"
        assert stored["point_id"] == "point-retention"
        assert stored["schema"] == SCHEMA
        assert stored["raw_hash"]
        assert stored["status"] == "succeeded"
        assert stored["result"] is None
        assert repository.list_attempts(job["id"]) == []
    finally:
        database.close()
