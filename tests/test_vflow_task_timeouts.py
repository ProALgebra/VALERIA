from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from opencode_web.database import Database, utcnow
from opencode_web.vflow.gateway import ValeriaTaskGateway
from opencode_web.vflow.recovery import VFlowRecovery
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

TASK_SOURCE = """\
workflow "task-timeout" {
  version: 1;
  input: json;
  step work = task.run(task_id: "<TASK_ID>", input_path: "$", result_mode: "<RESULT_MODE>"<CONFIG>);
  start -> work -> end
}
"""


class ScriptedGateway:
    def __init__(self, task_status: dict):
        self.task_status = task_status
        self.enqueued: list[dict] = []
        self.cancelled: list[str] = []
        self._task_runs: dict[str, str] = {}

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        key = kwargs.get("idempotency_key") or f"{kwargs['run_id']}:{kwargs['point_id']}"
        if key not in self._task_runs:
            self._task_runs[key] = f"job-{len(self._task_runs) + 1}"
        return self._task_runs[key]

    def status(self, task_run_id: str):
        return dict(self.task_status)

    def cancel(self, task_run_id: str) -> bool:
        self.cancelled.append(task_run_id)
        return True


@contextmanager
def make_flow(
    tmp_path,
    gateway_factory,
    *,
    config: str = "",
    result_mode: str = "auto",
):
    database = Database(tmp_path / "task-timeouts.sqlite3", "secret")
    database.initialize()
    try:
        admin = database.create_user(
            "admin", "a-strong-password", "admin", str(tmp_path)
        )
        service = VFlowService(database)
        task = database.create_task(
            admin_id=admin["id"],
            title="Echo",
            description="",
            content="Return the input",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
            published=True,
        )
        source = (
            TASK_SOURCE.replace("<TASK_ID>", task["id"])
            .replace("<RESULT_MODE>", result_mode)
            .replace("<CONFIG>", config)
        )
        definition = service.create_definition(
            admin["id"], slug="task-timeout", title="task-timeout"
        )
        service.save_draft(definition["id"], admin["id"], source=source)
        service.publish(definition["id"], admin["id"])
        version = service.repository.get_published(definition["id"])
        assert version is not None
        gateway = gateway_factory(database)
        runtime = VFlowRuntime(database, gateway)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={"payload": "hello"},
        )
        yield database, runtime, run, gateway
    finally:
        database.close()


def force_due(database):
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET next_check_at = ? WHERE status = 'waiting_task'",
            (utcnow(),),
        )


def age_column(database, column: str, seconds: int = 60):
    value = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE vflow_task_links SET {column} = ?",  # noqa: S608 - fixed column names
            (value,),
        )


def test_queue_timeout_retries_as_separate_task_run(tmp_path):
    scripted = ScriptedGateway({"status": "queued"})
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", queue_timeout_seconds: 5, retries: 1",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        links = database._all(
            "SELECT * FROM vflow_task_links ORDER BY attempt"
        )
        assert [link["attempt"] for link in links] == [1]
        assert links[0]["status"] == "queued"

        age_column(database, "queued_at")
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "running"
        assert scripted.cancelled == ["job-1"]
        assert len(scripted.enqueued) == 2
        keys = [item["idempotency_key"] for item in scripted.enqueued]
        assert len(set(keys)) == 2
        assert keys[1].endswith(":2")

        links = database._all(
            "SELECT * FROM vflow_task_links ORDER BY attempt"
        )
        assert [link["attempt"] for link in links] == [1, 2]
        assert links[0]["status"] == "timed_out"
        assert links[0]["finished_at"] is not None
        assert links[1]["status"] == "queued"
        point = database._one("SELECT * FROM vflow_points WHERE id = ?", (links[1]["point_id"],))
        assert point["attempt"] == 2
        assert point["task_run_id"] == "job-2"

        outbox = database._all("SELECT * FROM vflow_outbox ORDER BY created_at")
        assert len(outbox) == 2
        assert {item["status"] for item in outbox} == {"sent"}

        VFlowRecovery(database).run_once()
        runtime.run_until_idle(run["id"])
        assert (
            database._one("SELECT COUNT(*) AS count FROM vflow_task_links")["count"] == 2
        )
        assert database._one("SELECT COUNT(*) AS count FROM vflow_outbox")["count"] == 2
        assert len(scripted.enqueued) == 2


def test_queue_timeout_retry_creates_second_queue_job(tmp_path):
    with make_flow(
        tmp_path,
        ValeriaTaskGateway,
        config=", queue_timeout_seconds: 5, retries: 1",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        age_column(database, "queued_at")
        force_due(database)

        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "running"
        links = database._all(
            "SELECT * FROM vflow_task_links ORDER BY attempt"
        )
        assert [(link["attempt"], link["status"]) for link in links] == [
            (1, "timed_out"),
            (2, "queued"),
        ]
        jobs = database._all(
            "SELECT status FROM queue_jobs WHERE message_id LIKE 'msg_vflow_%' "
            "ORDER BY sequence"
        )
        assert [job["status"] for job in jobs] == ["cancelled", "queued"]


def test_queue_timeout_without_retries_fails_run_and_cancels_job(tmp_path):
    with make_flow(
        tmp_path,
        ValeriaTaskGateway,
        config=", queue_timeout_seconds: 5",
    ) as (database, runtime, run, gateway):
        del gateway
        runtime.run_until_idle(run["id"])
        link = database._one("SELECT * FROM vflow_task_links")
        assert link is not None
        job_id = link["task_run_id"]
        assert database.get_job(job_id)["status"] == "queued"

        age_column(database, "queued_at")
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_QUEUE_TIMEOUT"
        links = database._all("SELECT * FROM vflow_task_links")
        assert len(links) == 1
        assert links[0]["status"] == "timed_out"
        jobs = database._all(
            "SELECT status FROM queue_jobs WHERE message_id LIKE 'msg_vflow_%'"
        )
        assert [job["status"] for job in jobs] == ["cancelled"]
        assert database._one("SELECT COUNT(*) AS count FROM vflow_outbox")["count"] == 1
        point = database._one("SELECT * FROM vflow_points")
        assert point["status"] == "cancelled"


def test_execution_timeout_fails_without_retry(tmp_path):
    scripted = ScriptedGateway({"status": "running"})
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", execution_timeout_seconds: 5, retries: 3",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        force_due(database)
        runtime.run_until_idle(run["id"])

        link = database._one("SELECT * FROM vflow_task_links")
        assert link["started_at"] is not None
        assert len(scripted.enqueued) == 1

        age_column(database, "started_at")
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_EXECUTION_TIMEOUT"
        assert scripted.cancelled == ["job-1"]
        assert len(scripted.enqueued) == 1
        link = database._one("SELECT * FROM vflow_task_links")
        assert link["status"] == "timed_out"


def test_started_task_is_not_subject_to_queue_timeout(tmp_path):
    scripted = ScriptedGateway({"status": "running"})
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", queue_timeout_seconds: 5",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        age_column(database, "queued_at")
        force_due(database)

        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "running"
        assert scripted.cancelled == []
        link = database._one("SELECT * FROM vflow_task_links")
        assert link["started_at"] is not None
        assert link["status"] == "queued"


def test_task_changed_is_not_retried(tmp_path):
    scripted = ScriptedGateway(
        {
            "status": "failed",
            "code": "TASK_CHANGED",
            "safe_error": "Task changed after the run started",
        }
    )
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", retries: 3",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_CHANGED"
        assert len(scripted.enqueued) == 1
        link = database._one("SELECT * FROM vflow_task_links")
        assert link["status"] == "failed"


def test_cancelled_task_is_not_retried(tmp_path):
    scripted = ScriptedGateway(
        {"status": "cancelled", "safe_error": "Task was cancelled"}
    )
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", retries: 3",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_CANCELLED"
        assert len(scripted.enqueued) == 1


def test_invalid_json_output_is_not_retried(tmp_path):
    scripted = ScriptedGateway({"status": "completed", "text": "not json"})
    with make_flow(
        tmp_path,
        lambda database: scripted,
        config=", retries: 3",
        result_mode="strict_json",
    ) as (database, runtime, run, _):
        runtime.run_until_idle(run["id"])
        force_due(database)
        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "failed"
        assert finished["error_code"] == "TASK_OUTPUT_NOT_JSON"
        assert len(scripted.enqueued) == 1
