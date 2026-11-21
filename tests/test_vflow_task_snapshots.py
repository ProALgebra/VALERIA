from __future__ import annotations

import json

import pytest

from opencode_web.database import Database
from opencode_web.vflow.api import TASK_SNAPSHOT_FIELDS, _admin_run_view, _run_view
from opencode_web.vflow.gateway import ValeriaTaskGateway
from opencode_web.vflow.runtime import VFlowRuntime, VFlowRuntimeError
from opencode_web.vflow.service import VFlowService

TASK_SOURCE = """\
workflow "task-snapshot" {
  version: 1;
  input: json;
  step work = task.run(task_id: "<TASK_ID>", input_path: "$", result_mode: "auto");
  start -> work -> end
}
"""

TWO_TASK_SOURCE = """\
workflow "two-task-snapshots" {
  version: 1;
  input: json;
  step first = task.run(task_id: "<FIRST_ID>", input_path: "$", result_mode: "auto");
  step second = task.run(task_id: "<SECOND_ID>", input_path: "$", result_mode: "auto");
  start -> first -> second -> end
}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "task-snapshots.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    return database, admin, VFlowService(database)


def create_task(
    database,
    admin,
    *,
    title="Echo",
    content="Prompt used by the task",
    agent="build",
    provider_id=None,
    model_id=None,
    model_variant=None,
):
    return database.create_task(
        admin_id=admin["id"],
        title=title,
        description="",
        content=content,
        agent=agent,
        provider_id=provider_id,
        model_id=model_id,
        model_variant=model_variant,
        published=True,
    )


def publish(service, admin, slug, source):
    definition = service.create_definition(admin["id"], slug=slug, title=slug)
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def test_create_run_snapshots_every_task_node_and_hides_them_from_users(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        first = create_task(
            database,
            admin,
            title="First task",
            content="First private prompt",
            provider_id="openai",
            model_id="gpt-4",
            model_variant="mini",
        )
        second = create_task(
            database,
            admin,
            title="Second task",
            content="Second private prompt",
            agent="plan",
        )
        source = TWO_TASK_SOURCE.replace("<FIRST_ID>", first["id"]).replace(
            "<SECOND_ID>", second["id"]
        )
        definition, version = publish(service, admin, "two-tasks", source)

        runtime = VFlowRuntime(database)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"question": "hello"},
        )

        stored = database._one(
            "SELECT task_snapshots_json FROM vflow_runs WHERE id = ?", (run["id"],)
        )
        snapshots = json.loads(stored["task_snapshots_json"])
        assert set(snapshots) == {"first", "second"}
        assert snapshots["first"] == {
            "task_id": first["id"],
            "title": "First task",
            "updated_at": first["updated_at"],
            "agent": "build",
            "provider_id": "openai",
            "model_id": "gpt-4",
            "model_variant": "mini",
            "queue_id": "default",
        }
        assert snapshots["second"]["task_id"] == second["id"]
        assert snapshots["second"]["agent"] == "plan"
        assert "First private prompt" not in stored["task_snapshots_json"]
        assert "Second private prompt" not in stored["task_snapshots_json"]

        loaded = runtime.get_run(run["id"])
        user_view = _run_view(database, loaded)
        assert "task_snapshots" not in user_view
        assert "task_snapshots_json" not in user_view
        admin_detail = _admin_run_view(database, loaded, details=True)
        assert admin_detail["task_snapshots"]["first"] == snapshots["first"]
        assert set(admin_detail["task_snapshots"]["first"]) == set(
            TASK_SNAPSHOT_FIELDS
        )
        assert "content" not in admin_detail["task_snapshots"]["first"]
    finally:
        database.close()


def test_create_run_rejects_deleted_task_before_creating_any_run(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        task = create_task(database, admin)
        definition, version = publish(
            service,
            admin,
            "deleted-task",
            TASK_SOURCE.replace("<TASK_ID>", task["id"]),
        )
        assert database.delete_task(task["id"]) is True

        runtime = VFlowRuntime(database)
        with pytest.raises(VFlowRuntimeError) as error:
            runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )
        assert error.value.code == "TASK_UNAVAILABLE"
        assert database._one("SELECT COUNT(*) AS count FROM vflow_runs")["count"] == 0
        assert database._one("SELECT COUNT(*) AS count FROM vflow_points")["count"] == 0
    finally:
        database.close()


def test_task_changed_after_run_start_fails_without_queue_job(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        task = create_task(database, admin, content="Original prompt")
        definition, version = publish(
            service,
            admin,
            "changed-task",
            TASK_SOURCE.replace("<TASK_ID>", task["id"]),
        )
        runtime = VFlowRuntime(database, ValeriaTaskGateway(database))
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"question": "hello"},
        )

        updated = database.update_task(task["id"], {"content": "Changed prompt"})
        assert updated is not None
        assert updated["updated_at"] != task["updated_at"]

        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_CHANGED"
        assert database._one("SELECT COUNT(*) AS count FROM queue_jobs")["count"] == 0
        assert database._one("SELECT COUNT(*) AS count FROM chats")["count"] == 0
        assert (
            database._one("SELECT COUNT(*) AS count FROM vflow_task_links")["count"]
            == 0
        )
        outbox = database._one("SELECT status FROM vflow_outbox")
        assert outbox["status"] == "dead"
    finally:
        database.close()
