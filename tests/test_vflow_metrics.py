from __future__ import annotations

from opencode_web.database import Database
from opencode_web.vflow.metrics import VFlowMetrics
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "metrics" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""


def test_metrics_snapshot_counts_completed_run(tmp_path):
    database = Database(tmp_path / "metrics.sqlite3", "secret")
    database.initialize()
    try:
        admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
        service = VFlowService(database)
        definition = service.create_definition(
            admin["id"], slug="metrics-flow", title="Metrics flow"
        )
        service.save_draft(definition["id"], admin["id"], source=SOURCE)
        service.publish(definition["id"], admin["id"])
        version = service.repository.get_published(definition["id"])
        runtime = VFlowRuntime(database)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded"

        snapshot = VFlowMetrics(database).snapshot()
        assert snapshot["runs"]["succeeded"] == 1
        assert snapshot["points"]["consumed"] >= 1
        assert snapshot["active_points"] == 0
        assert snapshot["open_forks"] == 0
        assert snapshot["queue"].get("queued", 0) == 0
        assert snapshot["outbox"] == {}
    finally:
        database.close()
