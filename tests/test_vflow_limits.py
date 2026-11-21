from __future__ import annotations

import pytest

from opencode_web.database import Database
from opencode_web.vflow.limits import VFlowLimits
from opencode_web.vflow.runtime import VFlowRuntime, VFlowRuntimeError
from opencode_web.vflow.service import VFlowService

SIMPLE_SOURCE = """\
workflow "limit-flow" {
  version: 1;
  input: json;
  step pass = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> pass -> end
}
"""

FORK_SOURCE = """\
workflow "fork-limit" {
  version: 1;
  input: json;
  step split = fork(count: 2, join: combined);
  step left = transform(operations: [
    {op: "set", path: "$.left", value: true}
  ]);
  step right = transform(operations: [
    {op: "set", path: "$.right", value: true}
  ]);
  step combined = merge();
  start -> split;
  split[0] -> left -> combined;
  split[1] -> right -> combined;
  combined -> end
}
"""


def make_run(tmp_path, source=SIMPLE_SOURCE, limits=None):
    database = Database(tmp_path / "limits.sqlite3", "secret")
    database.initialize()
    admin = database.create_user(
        "admin", "a-strong-password", "admin", str(tmp_path)
    )
    service = VFlowService(database)
    definition = service.create_definition(admin["id"], slug="limits", title="Limits")
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    runtime = VFlowRuntime(database, limits=limits or VFlowLimits())
    return database, admin, definition, version, runtime


def test_input_json_bytes_limit(tmp_path):
    database, admin, definition, version, runtime = make_run(
        tmp_path, limits=VFlowLimits(max_json_bytes=10)
    )
    try:
        with pytest.raises(VFlowRuntimeError) as exc:
            runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={"long": "value"},
            )
        assert exc.value.code == "LIMIT_JSON_BYTES"
    finally:
        database.close()


def test_input_json_depth_limit(tmp_path):
    database, admin, definition, version, runtime = make_run(
        tmp_path, limits=VFlowLimits(max_json_depth=1)
    )
    try:
        with pytest.raises(VFlowRuntimeError) as exc:
            runtime.create_run(
                definition_id=definition["id"],
                version_id=version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={"a": {"b": 1}},
            )
        assert exc.value.code == "LIMIT_JSON_DEPTH"
    finally:
        database.close()


def test_fork_count_limit(tmp_path):
    database, admin, definition, version, runtime = make_run(
        tmp_path, source=FORK_SOURCE, limits=VFlowLimits(max_fork_n=1)
    )
    try:
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "LIMIT_FORK_COUNT"
    finally:
        database.close()


def test_active_point_budget(tmp_path):
    database, admin, definition, version, runtime = make_run(
        tmp_path, source=FORK_SOURCE, limits=VFlowLimits(max_active_points=1)
    )
    try:
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "LIMIT_ACTIVE_POINTS"
    finally:
        database.close()


def test_transition_limit(tmp_path):
    database, admin, definition, version, runtime = make_run(
        tmp_path, limits=VFlowLimits(max_transitions=0)
    )
    try:
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "LIMIT_TRANSITIONS"
    finally:
        database.close()
