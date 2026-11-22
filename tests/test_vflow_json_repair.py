from __future__ import annotations

import json

import pytest

from opencode_web.database import Database, utcnow
from opencode_web.vflow.api import _admin_run_view, _run_view
from opencode_web.vflow.gateway import ValeriaTaskGateway, VFlowTaskError
from opencode_web.vflow.json_schema import (
    SchemaDefinitionError,
    build_correction_prompt,
    validate_output,
    validate_schema,
)
from opencode_web.vflow.models import CompileError
from opencode_web.vflow.registry import DEFAULT_REGISTRY, TASK
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

TASK_SOURCE = """\
workflow "repair-flow" {{
  version: 1;
  input: json;
  step work = task.run(
    task_id: "{task_id}",
    input_path: "$",
    result_mode: "strict_json",
    json_schema: {schema},
    max_json_repairs: {max_repairs}
  );
  start -> work -> end
}}
"""

TWO_TASKS_SOURCE = """\
workflow "repair-two" {{
  version: 1;
  input: json;
  step first = task.run(
    task_id: "{first}",
    input_path: "$",
    result_mode: "strict_json",
    json_schema: {schema},
    max_json_repairs: 1
  );
  step second = task.run(task_id: "{second}", input_path: "$");
  start -> first -> second -> end
}}
"""

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


def make_env(tmp_path):
    database = Database(tmp_path / "json-repair.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    worker = database.create_user(
        "worker", "a-strong-password", "user", str(tmp_path / "workspace")
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return database, admin, worker, VFlowService(database)


def create_task(database, admin, title="Echo"):
    return database.create_task(
        admin_id=admin["id"],
        title=title,
        description="",
        content="Return JSON",
        agent="build",
        provider_id="openai",
        model_id="gpt-4",
        model_variant="high",
        published=True,
    )


def task_source(task_id, *, schema=SCHEMA, max_repairs=1):
    return TASK_SOURCE.format(
        task_id=task_id,
        schema=json.dumps(schema, ensure_ascii=False),
        max_repairs=max_repairs,
    )


def publish(service, admin, task_id, *, slug="repair-flow", source=None):
    definition = service.create_definition(admin["id"], slug=slug, title=slug)
    service.save_draft(
        definition["id"],
        admin["id"],
        source=source or task_source(task_id),
    )
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def force_due(database):
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
            (utcnow(),),
        )


def drain(runtime, run_id, database, rounds=8):
    run = runtime.get_run(run_id)
    for _ in range(rounds):
        runtime.run_until_idle(run_id)
        run = runtime.get_run(run_id)
        if run["status"] != "running":
            break
        force_due(database)
    return run


class RepairGateway:
    def __init__(self, outputs: list[str], **status_overrides):
        self.outputs = outputs
        self.status_overrides = status_overrides
        self.enqueued: list[dict] = []
        self.repairs: list[dict] = []
        self.cancelled: list[str] = []
        self._next_job = 0

    def _job_id(self):
        self._next_job += 1
        return f"job-{self._next_job}"

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return self._job_id()

    def status(self, task_run_id: str):
        override = self.status_overrides.get(task_run_id)
        if override is not None:
            return override
        try:
            index = int(task_run_id.split("-")[1]) - 1
            text = self.outputs[index]
        except (IndexError, ValueError):
            return {"status": "failed", "safe_error": "missing output"}
        return {"status": "completed", "text": text}

    def repair_output(self, **kwargs):
        self.repairs.append(kwargs)
        return self._job_id()

    def cancel(self, task_run_id: str) -> None:
        self.cancelled.append(task_run_id)


def _point(database, run_id):
    return database._one("SELECT * FROM vflow_points WHERE run_id = ?", (run_id,))


def test_task_json_schema_is_validated_in_registry_and_compiler(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        spec = DEFAULT_REGISTRY.get(TASK)
        assert "json_schema" in spec.config_schema["properties"]
        assert spec.config_schema["properties"]["json_schema"]["title"] == "Требуемый JSON"
        repairs = spec.config_schema["properties"]["max_json_repairs"]
        assert repairs["minimum"] == 0
        assert repairs["maximum"] == 3
        assert repairs["default"] == 1
        assert repairs["title"] == "Попыток исправления"

        definition = service.create_definition(admin["id"], slug="valid-schema", title="Valid")
        saved = service.save_draft(
            definition["id"],
            admin["id"],
            source=task_source(task["id"], max_repairs=2),
        )
        node = next(
            item
            for item in saved["graph"]["nodes"]
            if item["type"] == TASK
        )
        assert node["config"]["max_json_repairs"] == 2
        assert node["config"]["json_schema"] == SCHEMA

        with pytest.raises(CompileError) as invalid_schema:
            service.save_draft(
                definition["id"],
                admin["id"],
                source=task_source(task["id"], schema={"type": "wat"}),
            )
        assert "COMPILE_INVALID_JSON_SCHEMA" in {
            item.code for item in invalid_schema.value.diagnostics
        }

        with pytest.raises(CompileError) as invalid_repairs:
            service.save_draft(
                definition["id"],
                admin["id"],
                source=task_source(task["id"], max_repairs=4),
            )
        assert "COMPILE_INVALID_MAX_JSON_REPAIRS" in {
            item.code for item in invalid_repairs.value.diagnostics
        }

        with pytest.raises(SchemaDefinitionError):
            validate_schema(
                {"type": "object", "properties": {"x": {"type": "array", "items": "bad"}}}
            )
    finally:
        database.close()


def test_json_validator_reports_safe_paths_and_no_schema_only_checks_syntax():
    value, failure = validate_output('{"ok": true}')
    assert value == {"ok": True}
    assert failure is None

    value, failure = validate_output("not json")
    assert value is None
    assert failure.code == "TASK_OUTPUT_NOT_JSON"

    value, failure = validate_output('{"items": [{"n": "x"}]}', {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                },
            }
        },
    })
    assert value == {"items": [{"n": "x"}]}
    assert failure.code == "TASK_SCHEMA_INVALID"
    assert failure.path == "$.items[0].n"
    assert "$.items[0].n" in failure.message

    _value, failure = validate_output('{"ok": true, "extra": 1}', SCHEMA)
    assert failure.path == "$.extra"
    assert "additional property" in failure.message

    prompt = build_correction_prompt(
        failure, SCHEMA
    )
    assert "Return only valid JSON" in prompt
    assert "markdown" in prompt
    assert "additionalProperties" in prompt


def test_runtime_repairs_invalid_json_and_advances(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(service, admin, task["id"])
        gateway = RepairGateway(["not json", '{"ok": true}'])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "succeeded", finished
        assert json.loads(finished["final_result_json"])["task"]["output"]["json"] == {
            "ok": True
        }
        assert len(gateway.repairs) == 1
        repair = gateway.repairs[0]
        assert repair["task_run_id"] == "job-1"
        assert repair["repair_attempt"] == 1
        assert "Return only valid JSON" in repair["prompt"]
        assert "not json" not in repair["prompt"]

        point = _point(database, run["id"])
        assert point["json_repair_attempt"] == 1
        links = database._all(
            "SELECT * FROM vflow_task_links ORDER BY repair_attempt, id"
        )
        assert len(links) == 2
        assert [link["status"] for link in links] == ["completed", "completed"]
        assert links[1]["repair_attempt"] == 1
        assert links[1]["parent_task_run_id"] == links[0]["task_run_id"]
        assert links[1]["repair_error_code"] == "TASK_OUTPUT_NOT_JSON"
        events = [
            row["event_type"]
            for row in database._all(
                "SELECT event_type FROM vflow_events WHERE run_id = ? ORDER BY id",
                (run["id"],),
            )
        ]
        assert "TASK_JSON_REPAIR_REQUESTED" in events
        assert "TASK_JSON_REPAIR_COMPLETED" in events
    finally:
        database.close()


def test_runtime_schema_mismatch_requests_repair(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(service, admin, task["id"])
        gateway = RepairGateway(['{"ok": "yes"}', '{"ok": true}'])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "succeeded", finished
        repair = database._one(
            "SELECT * FROM vflow_task_links WHERE repair_attempt = 1"
        )
        assert repair["repair_error_code"] == "TASK_SCHEMA_INVALID"
        assert "$.ok" in gateway.repairs[0]["prompt"]
        assert "expected boolean" in gateway.repairs[0]["prompt"]
    finally:
        database.close()


def test_runtime_repair_limit_and_max_zero(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service,
            admin,
            task["id"],
            slug="repair-limit",
            source=task_source(task["id"], max_repairs=1),
        )
        gateway = RepairGateway(["not json", "still not json"])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_OUTPUT_NOT_JSON"
        assert len(gateway.repairs) == 1

        zero = publish(
            service,
            admin,
            task["id"],
            slug="repair-zero",
            source=task_source(task["id"], max_repairs=0),
        )
        zero_gateway = RepairGateway(["not json", '{"ok": true}'])
        zero_runtime = VFlowRuntime(database, zero_gateway)
        zero_run = zero_runtime.create_run(
            definition_id=zero[0]["id"],
            version_id=zero[1]["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(zero_runtime, zero_run["id"], database)
        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_OUTPUT_NOT_JSON"
        assert zero_gateway.repairs == []
    finally:
        database.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ({"status": "cancelled", "safe_error": "cancelled"}, "TASK_CANCELLED"),
        (
            {"status": "failed", "code": "TASK_CHANGED", "safe_error": "changed"},
            "TASK_CHANGED",
        ),
        (
            {"status": "failed", "code": "TASK_UNAVAILABLE", "safe_error": "gone"},
            "TASK_UNAVAILABLE",
        ),
        (
            {"status": "failed", "code": "assistant_error", "safe_error": "boom"},
            "TASK_FAILED",
        ),
    ],
)
def test_runtime_does_not_repair_non_output_failures(tmp_path, status, code):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service,
            admin,
            task["id"],
            slug=f"no-repair-{code.lower().replace('_', '-')}",
        )
        gateway = RepairGateway(["not json"], **{"job-1": status})
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "failed"
        assert finished["error_code"] == code
        assert gateway.repairs == []
    finally:
        database.close()


def test_runtime_does_not_repair_task_queue_timeout(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        source = task_source(task["id"]).replace(
            'result_mode: "strict_json",',
            'result_mode: "strict_json", queue_timeout_seconds: 1,',
        )
        definition, version = publish(
            service, admin, task["id"], slug="repair-timeout", source=source
        )
        gateway = RepairGateway(["not json"], **{"job-1": {"status": "queued"}})
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        with database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_task_links SET queued_at = ? WHERE task_run_id = 'job-1'",
                ("2000-01-01T00:00:00+00:00",),
            )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_QUEUE_TIMEOUT"
        assert gateway.repairs == []
    finally:
        database.close()


def test_runtime_repair_job_timeout_fails_without_another_repair(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        source = task_source(task["id"]).replace(
            'result_mode: "strict_json",',
            'result_mode: "strict_json", queue_timeout_seconds: 1,',
        )
        definition, version = publish(
            service, admin, task["id"], slug="repair-job-timeout", source=source
        )
        gateway = RepairGateway(["not json", '{"ok": true}'])
        runtime = VFlowRuntime(database, gateway)
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
        point = _point(database, run["id"])
        repair_id = point["task_run_id"]
        assert point["json_repair_attempt"] == 1
        assert len(gateway.repairs) == 1
        gateway.status_overrides[repair_id] = {"status": "queued"}
        with database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_task_links SET queued_at = ?
                   WHERE task_run_id = ?""",
                ("2000-01-01T00:00:00+00:00", repair_id),
            )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_QUEUE_TIMEOUT"
        assert len(gateway.repairs) == 1
        links = database._all(
            "SELECT status FROM vflow_task_links WHERE point_id = ?", (point["id"],)
        )
        assert {link["status"] for link in links} == {"timed_out"}
    finally:
        database.close()


def test_json_repair_migrations_are_idempotent(tmp_path):
    path = tmp_path / "migrate.sqlite3"
    database = Database(path, "secret")
    database.initialize()
    database.close()
    database = Database(path, "secret")
    database.initialize()
    try:
        point_columns = {
            row[1]
            for row in database._connection.execute(
                "PRAGMA table_info(vflow_points)"
            ).fetchall()
        }
        link_columns = {
            row[1]
            for row in database._connection.execute(
                "PRAGMA table_info(vflow_task_links)"
            ).fetchall()
        }
        job_columns = {
            row[1]
            for row in database._connection.execute(
                "PRAGMA table_info(queue_jobs)"
            ).fetchall()
        }
        assert "json_repair_attempt" in point_columns
        assert {"repair_attempt", "parent_task_run_id"} <= link_columns
        assert {"job_kind", "repair_attempt", "parent_task_run_id"} <= job_columns
        assert database._one(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE name = 'consultation_artifacts'"
        )["c"] == 1
    finally:
        database.close()


def test_sequential_tasks_still_enqueue_after_repair(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        first = create_task(database, admin, "First")
        second = create_task(database, admin, "Second")
        source = TWO_TASKS_SOURCE.format(
            first=first["id"],
            second=second["id"],
            schema=json.dumps(SCHEMA),
        )
        definition, version = publish(
            service, admin, first["id"], slug="repair-two", source=source
        )
        gateway = RepairGateway(["not json", '{"ok": true}', '{"done": true}'])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = drain(runtime, run["id"], database)
        assert finished["status"] == "succeeded", finished
        assert [item["task_id"] for item in gateway.enqueued] == [
            first["id"],
            second["id"],
        ]
        assert len(gateway.repairs) == 1
        assert json.loads(finished["final_result_json"])["task"]["output"]["json"] == {
            "done": True
        }
    finally:
        database.close()


def _completed_original(database, gateway, admin, worker):
    task = create_task(database, admin)
    job_id = gateway.enqueue(
        run_id="run-1",
        point_id="point-1",
        task_id=task["id"],
        user_id=worker["id"],
        workspace_id=worker["workspace_path"],
        input_data={},
        result_mode="strict_json",
        node_id="work",
    )
    database.complete_task_run(job_id, {"message_id": "m-1", "text": "not json"})
    return task, job_id


def test_gateway_repair_creates_one_same_chat_followup(tmp_path):
    database, admin, worker, _service = make_env(tmp_path)
    try:
        gateway = ValeriaTaskGateway(database)
        task, original_id = _completed_original(database, gateway, admin, worker)
        original = database.get_job(original_id)

        repair_id = gateway.repair_output(
            task_run_id=original_id,
            run_id="run-1",
            point_id="point-1",
            repair_attempt=1,
            prompt="fix JSON",
        )
        repeated = gateway.repair_output(
            task_run_id=original_id,
            run_id="run-1",
            point_id="point-1",
            repair_attempt=1,
            prompt="fix JSON",
        )
        assert repeated == repair_id
        job = database.get_job(repair_id)
        assert job["chat_id"] == original["chat_id"]
        assert job["session_id"] == original["session_id"]
        assert job["task_id"] is None
        assert job["pipeline_json"] == ""
        assert job["job_kind"] == "vflow_repair"
        assert job["repair_attempt"] == 1
        assert job["parent_task_run_id"] == original_id
        assert job["vflow_run_id"] == "run-1"
        assert job["vflow_point_id"] == "point-1"
        assert job["origin_node_id"] == "work"
        assert job["agent"] == original["agent"]
        assert job["provider_id"] == original["provider_id"]
        assert job["model_id"] == original["model_id"]
        assert job["model_variant"] == original["model_variant"]
        assert job["queue_id"] == original["queue_id"]
        assert job["message_id"].startswith("msg_vflow_repair_")
        assert job["message_id"] == (
            "msg_vflow_repair_"
            + __import__("hashlib").sha256(b"run-1:point-1:1").hexdigest()[:40]
        )
        assert database._one("SELECT COUNT(*) AS c FROM queue_jobs")["c"] == 2
        assert task["id"] == original["task_id"]

        database.complete_vflow_repair_run(
            repair_id, {"message_id": "m-2", "text": '{"ok": true}'}
        )
        assert gateway.status(repair_id) == {
            "status": "completed",
            "text": '{"ok": true}',
        }
        assert database._all(
            "SELECT id FROM queue_jobs WHERE parent_job_id = ?", (repair_id,)
        ) == []
    finally:
        database.close()


def test_gateway_repair_rejects_foreign_or_incomplete_run(tmp_path):
    database, admin, worker, _service = make_env(tmp_path)
    try:
        gateway = ValeriaTaskGateway(database)
        task, original_id = _completed_original(database, gateway, admin, worker)
        with pytest.raises(VFlowTaskError) as mismatch:
            gateway.repair_output(
                task_run_id=original_id,
                run_id="other-run",
                point_id="point-1",
                repair_attempt=1,
                prompt="fix",
            )
        assert mismatch.value.code == "TASK_REPAIR_UNAVAILABLE"

        queued = gateway.enqueue(
            run_id="run-2",
            point_id="point-2",
            task_id=task["id"],
            user_id=worker["id"],
            workspace_id=worker["workspace_path"],
            input_data={},
            result_mode="strict_json",
        )
        with pytest.raises(VFlowTaskError) as incomplete:
            gateway.repair_output(
                task_run_id=queued,
                run_id="run-2",
                point_id="point-2",
                repair_attempt=1,
                prompt="fix",
            )
        assert incomplete.value.code == "TASK_REPAIR_UNAVAILABLE"
        assert database._one("SELECT COUNT(*) AS c FROM queue_jobs")["c"] == 2
    finally:
        database.close()


def test_repair_outbox_reconciliation_is_idempotent(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service, admin, task["id"], slug="repair-reconcile"
        )
        gateway = RepairGateway(["not json", '{"ok": true}'])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        force_due(database)
        point = _point(database, run["id"])
        link = database._one(
            "SELECT * FROM vflow_task_links WHERE task_run_id = 'job-1'"
        )
        payload = {
            "task_run_id": "job-1",
            "task_id": link["task_id"],
            "run_id": run["id"],
            "point_id": point["id"],
            "node_id": point["node_id"],
            "visit_seq": point["visit_seq"],
            "attempt": point["attempt"],
            "repair_attempt": 1,
            "prompt": "fix JSON",
            "error_code": "TASK_OUTPUT_NOT_JSON",
            "error_message": "$: not valid",
            "error_path": "$",
        }
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO vflow_outbox
                   (id, dedupe_key, run_id, point_id, kind, payload_json, status,
                    attempts, next_attempt_at, created_at)
                   VALUES ('outbox-repair', 'repair-key', ?, ?, 'task_json_repair',
                           ?, 'pending', 0, ?, ?)""",
                (run["id"], point["id"], json.dumps(payload), utcnow(), utcnow()),
            )

        assert runtime.reconcile_repair_outbox() == 1
        assert runtime.reconcile_repair_outbox() == 0
        assert len(gateway.repairs) == 1
        saved_point = _point(database, run["id"])
        assert saved_point["task_run_id"] == "job-2"
        assert saved_point["json_repair_attempt"] == 1
        outbox = database._one("SELECT * FROM vflow_outbox WHERE id = 'outbox-repair'")
        assert outbox["status"] == "sent"
        assert database._one("SELECT COUNT(*) AS c FROM vflow_task_links")["c"] == 2
    finally:
        database.close()


def test_cancel_run_cancels_queued_repair_job(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service, admin, task["id"], slug="repair-cancel"
        )
        gateway = ValeriaTaskGateway(database)
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        runtime.run_until_idle(run["id"])
        original_id = _point(database, run["id"])["task_run_id"]
        database.complete_task_run(
            original_id, {"message_id": "m-1", "text": "not json"}
        )
        force_due(database)
        runtime.run_until_idle(run["id"])
        point = _point(database, run["id"])
        repair_id = point["task_run_id"]
        assert repair_id != original_id
        assert database.get_job(repair_id)["job_kind"] == "vflow_repair"
        assert database.get_job(repair_id)["status"] == "queued"

        assert runtime.cancel_run(run["id"]) is True
        assert database.get_job(repair_id)["status"] == "cancelled"
        link = database._one(
            "SELECT * FROM vflow_task_links WHERE task_run_id = ?", (repair_id,)
        )
        assert link["status"] == "cancelled"
    finally:
        database.close()


def test_admin_repair_diagnostics_do_not_leak_to_user_view(tmp_path):
    database, admin, _worker, service = make_env(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service, admin, task["id"], slug="repair-admin"
        )
        gateway = RepairGateway(["not json", '{"ok": true}'])
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        drain(runtime, run["id"], database)
        saved_run = runtime.get_run(run["id"])
        detail = _admin_run_view(database, saved_run, details=True)
        assert detail["points"][0]["json_repair_attempt"] == 1
        assert detail["json_repair_attempts"] == 1
        assert len(detail["task_repairs"]) == 1
        repair = detail["task_repairs"][0]
        assert repair["repair_attempt"] == 1
        assert repair["repair_error_code"] == "TASK_OUTPUT_NOT_JSON"
        assert repair["parent_task_run_id"] == "job-1"
        assert repair["status"] == "completed"

        user_view = _run_view(database, saved_run)
        assert "task_correlations" not in user_view
        assert "task_repairs" not in user_view
        assert "job-1" not in json.dumps(user_view)
        assert "not json" not in json.dumps(user_view)
    finally:
        database.close()
