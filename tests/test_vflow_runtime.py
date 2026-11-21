from __future__ import annotations

import json

from opencode_web.database import Database, utcnow
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SEQ_SOURCE = """\
workflow "review" {
  version: 1;
  input: json;
  step prepare = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  step decision = if(path: "$.ok", operator: "eq", value: true, missing: "error");
  step accepted = transform(operations: [
    {op: "set", path: "$.result", value: "accepted"}
  ]);
  step rejected = transform(operations: [
    {op: "set", path: "$.result", value: "rejected"}
  ]);
  start -> prepare -> decision;
  decision.true -> accepted -> end;
  decision.false -> rejected -> end
}
"""

FORK_SOURCE = """\
workflow "fanout" {
  version: 1;
  input: json;
  step split = fork(count: 2, join: combined);
  step left = transform(operations: [
    {op: "set", path: "$.side", value: "left"}
  ]);
  step right = transform(operations: [
    {op: "set", path: "$.side", value: "right"}
  ]);
  step combined = merge();
  start -> split;
  split[0] -> left -> combined;
  split[1] -> right -> combined;
  combined -> end
}
"""

TASK_SOURCE = """\
workflow "task-flow" {
  version: 1;
  input: json;
  step work = task.run(task_id: "<TASK_ID>", input_path: "$", result_mode: "strict_json");
  start -> work -> end
}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    return database, admin, VFlowService(database)


def publish(service, admin, slug, source):
    definition = service.create_definition(admin["id"], slug=slug, title=slug)
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def test_runtime_transform_if_and_end():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            definition, version = publish(service, admin, "review", SEQ_SOURCE)
            runtime = VFlowRuntime(database)
            run = runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            assert json.loads(finished["final_result_json"]) == {
                "ok": True,
                "result": "accepted",
            }
        finally:
            database.close()


def test_runtime_fork_merge_output_order():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            definition, version = publish(service, admin, "fanout", FORK_SOURCE)
            runtime = VFlowRuntime(database)
            run = runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={"seed": 1},
            )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert result["source"] == {"seed": 1}
            assert [item["index"] for item in result["branches"]] == [0, 1]
            assert [item["data"]["side"] for item in result["branches"]] == ["left", "right"]
        finally:
            database.close()


class FakeGateway:
    def __init__(self, text: str = '{"ok": true}'):
        self.text = text
        self.enqueued: list[dict] = []
        self.cancelled: list[str] = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return "job-1"

    def status(self, task_run_id: str):
        return {"status": "completed", "text": self.text}

    def cancel(self, task_run_id: str) -> None:
        self.cancelled.append(task_run_id)


def test_runtime_task_envelope_and_strict_json():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            task = database.create_task(
                admin_id=admin["id"],
                title="Echo",
                description="",
                content="Return JSON",
                agent="build",
                provider_id="openai",
                model_id="gpt-4",
                model_variant=None,
                published=True,
            )
            definition, version = publish(
                service,
                admin,
                "task-flow",
                TASK_SOURCE.replace("<TASK_ID>", task["id"]),
            )
            gateway = FakeGateway()
            runtime = VFlowRuntime(database, gateway)
            run = runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={"question": "hello"},
            )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "running", finished
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
                    (utcnow(),),
                )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert result["input"] == {"question": "hello"}
            assert result["task"]["output"]["json"] == {"ok": True}
            assert gateway.enqueued[0]["input_data"] == {"question": "hello"}
            snapshot = gateway.enqueued[0]["task_snapshot"]
            assert snapshot["task_id"] == task["id"]
            assert snapshot["updated_at"] == task["updated_at"]
        finally:
            database.close()


def test_runtime_strict_json_failure():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            task = database.create_task(
                admin_id=admin["id"],
                title="Echo",
                description="",
                content="Return JSON",
                agent="build",
                provider_id="openai",
                model_id="gpt-4",
                model_variant=None,
                published=True,
            )
            definition, version = publish(
                service,
                admin,
                "task-bad-json",
                TASK_SOURCE.replace("<TASK_ID>", task["id"]),
            )
            runtime = VFlowRuntime(database, FakeGateway("not json"))
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
                    "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
                    (utcnow(),),
                )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "failed"
            assert finished["error_code"] == "TASK_OUTPUT_NOT_JSON"
        finally:
            database.close()


def test_runtime_list_runs_filters_and_paginates():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            first, first_version = publish(service, admin, "list-first", SEQ_SOURCE)
            second, second_version = publish(service, admin, "list-second", SEQ_SOURCE)
            runtime = VFlowRuntime(database)

            run_a = runtime.create_run(
                definition_id=second["id"],
                version_id=second_version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )
            assert runtime.run_until_idle(run_a["id"])["status"] == "succeeded"
            run_b = runtime.create_run(
                definition_id=first["id"],
                version_id=first_version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )
            run_c = runtime.create_run(
                definition_id=first["id"],
                version_id=first_version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )
            runtime.cancel_run(run_c["id"])

            assert [run["id"] for run in runtime.list_runs(admin["id"])] == [
                run_c["id"],
                run_b["id"],
                run_a["id"],
            ]
            assert [
                run["id"] for run in runtime.list_runs(admin["id"], status="succeeded")
            ] == [run_a["id"]]
            assert [
                run["id"] for run in runtime.list_runs(admin["id"], status="running")
            ] == [run_b["id"]]
            assert [
                run["id"]
                for run in runtime.list_runs(admin["id"], definition_id=first["id"])
            ] == [run_c["id"], run_b["id"]]
            assert [
                run["id"] for run in runtime.list_runs(admin["id"], limit=1, offset=1)
            ] == [run_b["id"]]
            assert runtime.list_runs(admin["id"], offset=10) == []
        finally:
            database.close()


TWO_TASKS_SOURCE = """\
workflow "two-tasks" {
  version: 1;
  input: json;
  step first = task.run(task_id: "<TASK1>", input_path: "$");
  step second = task.run(task_id: "<TASK2>", input_path: "$");
  start -> first -> second -> end
}
"""


class CountingGateway:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self.cancelled: list[str] = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return f"job-{len(self.enqueued)}"

    def status(self, task_run_id: str):
        return {"status": "completed", "text": '{"ok": true}'}

    def cancel(self, task_run_id: str) -> None:
        self.cancelled.append(task_run_id)


def test_sequential_task_nodes_are_both_enqueued_and_clear_previous_link():
    """A reused point must not carry the previous Task run into the next node.

    Point rows are reused while a Run advances.  Before this regression test
    the old ``task_run_id`` survived the transition, so the second Task node
    was mistaken for an already completed attempt and was skipped entirely.
    """

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        database, admin, service = make_service(tmp_path)
        try:
            first = database.create_task(
                admin_id=admin["id"],
                title="First",
                description="",
                content="Return JSON",
                agent="build",
                provider_id="openai",
                model_id="gpt-4",
                model_variant=None,
                published=True,
            )
            second = database.create_task(
                admin_id=admin["id"],
                title="Second",
                description="",
                content="Return JSON",
                agent="build",
                provider_id="openai",
                model_id="gpt-4",
                model_variant=None,
                published=True,
            )
            definition, version = publish(
                service,
                admin,
                "two-tasks",
                TWO_TASKS_SOURCE.replace("<TASK1>", first["id"]).replace(
                    "<TASK2>", second["id"]
                ),
            )
            gateway = CountingGateway()
            runtime = VFlowRuntime(database, gateway)
            run = runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={},
            )

            runtime.run_until_idle(run["id"])
            assert len(gateway.enqueued) == 1
            assert gateway.enqueued[0]["task_id"] == first["id"]

            with database.transaction() as connection:
                connection.execute(
                    "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
                    (utcnow(),),
                )
            runtime.run_until_idle(run["id"])
            assert len(gateway.enqueued) == 2
            assert gateway.enqueued[1]["task_id"] == second["id"]

            with database.transaction() as connection:
                connection.execute(
                    "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
                    (utcnow(),),
                )
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            links = database._all(
                "SELECT task_id, status FROM vflow_task_links ORDER BY queued_at, id"
            )
            assert [(link["task_id"], link["status"]) for link in links] == [
                (first["id"], "completed"),
                (second["id"], "completed"),
            ]
        finally:
            database.close()
