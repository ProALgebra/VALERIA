from __future__ import annotations

import json

import pytest

from opencode_web.database import Database, utcnow
from opencode_web.vflow import compile_source
from opencode_web.vflow.limits import VFlowLimits
from opencode_web.vflow.models import CompileError
from opencode_web.vflow.recovery import VFlowRecovery
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

NESTED_SOURCE = """\
workflow "nested" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step splitB = fork(count: 2, join: mergedB);
  step left = transform(operations: [
    {op: "set", path: "$.side", value: "d"}
  ]);
  step right = transform(operations: [
    {op: "set", path: "$.side", value: "e"}
  ]);
  step other = transform(operations: [
    {op: "set", path: "$.side", value: "c"}
  ]);
  step mergedB = merge();
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> splitB;
  splitA[1] -> other -> mergedA;
  splitB[0] -> left -> mergedB;
  splitB[1] -> right -> mergedB;
  mergedB -> mergedA;
  mergedA -> end
}
"""

NESTED_IF_SOURCE = """\
workflow "nested-if" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step splitB = fork(count: 2, join: mergedB);
  step decideInner = if(path: "$.inner", operator: "eq", value: true, missing: "error");
  step decideOuter = if(path: "$.outer", operator: "eq", value: true, missing: "error");
  step mergedB = merge();
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> splitB;
  splitA[1] -> decideOuter;
  splitB[0] -> decideInner;
  splitB[1] -> mergedB;
  decideInner.true -> mergedB;
  decideInner.false -> mergedB;
  decideOuter.true -> mergedA;
  decideOuter.false -> mergedA;
  mergedB -> mergedA;
  mergedA -> end
}
"""

CYCLE_IF_SOURCE = """\
workflow "cycle-if" {
  version: 1;
  input: json;
  step decide = if(path: "$.done", operator: "eq", value: true, missing: "false");
  step mark = transform(operations: [
    {op: "set", path: "$.done", value: true}
  ]);
  start -> decide;
  decide.true -> end;
  decide.false -> mark -> decide
}
"""

CYCLE_FORK_SOURCE = """\
workflow "cycle-fork" {
  version: 1;
  input: json;
  step decide = if(path: "$.queue[0]", operator: "exists", missing: "false");
  step split = fork(count: 2, join: combined);
  step combined = merge();
  step pop = transform(operations: [
    {op: "select", path: "$.source"},
    {op: "remove", path: "$.queue[0]"}
  ]);
  start -> decide;
  decide.true -> split;
  decide.false -> end;
  split[0] -> combined;
  split[1] -> combined;
  combined -> pop -> decide
}
"""

CYCLE_LIMIT_SOURCE = """\
workflow "cycle-limit" {
  version: 1;
  input: json;
  step decide = if(path: "$.stop", operator: "eq", value: true, missing: "false");
  step tick = transform(operations: [
    {op: "set", path: "$.tick", value: true}
  ]);
  start -> decide;
  decide.true -> end;
  decide.false -> tick -> decide
}
"""

BYPASS_END_SOURCE = """\
workflow "bypass-end" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> end;
  splitA[1] -> mergedA;
  mergedA -> end
}
"""

IF_BYPASS_SOURCE = """\
workflow "if-bypass" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step decide = if(path: "$.ok", operator: "eq", value: true);
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> decide;
  splitA[1] -> mergedA;
  decide.true -> mergedA;
  decide.false -> end;
  mergedA -> end
}
"""

NESTED_CROSS_SOURCE = """\
workflow "nested-cross" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step splitB = fork(count: 2, join: mergedB);
  step mergedB = merge();
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> splitB;
  splitA[1] -> mergedA;
  splitB[0] -> mergedA;
  splitB[1] -> mergedB;
  mergedB -> mergedA;
  mergedA -> end
}
"""

UNPAIRED_MERGE_SOURCE = """\
workflow "unpaired-merge" {
  version: 1;
  input: json;
  step splitA = fork(count: 2, join: mergedA);
  step orphan = merge();
  step mergedA = merge();
  start -> splitA;
  splitA[0] -> mergedA;
  splitA[1] -> mergedA;
  mergedA -> orphan -> end
}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "nested.sqlite3", "secret")
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


def start_run(runtime, definition, version, admin, tmp_path, input_data):
    return runtime.create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=admin["id"],
        workspace_id=str(tmp_path),
        input_data=input_data,
    )


def points_at(database, run_id, node_id):
    return database._all(
        """SELECT * FROM vflow_points
           WHERE run_id = ? AND node_id = ?
           ORDER BY visit_seq, id""",
        (run_id, node_id),
    )


def stack_of(row):
    return json.loads(row["fork_stack_json"])


class Driver:
    """Process a Run one chosen point at a time for frame assertions."""

    def __init__(self, runtime, database, run_id):
        self.runtime = runtime
        self.database = database
        self.run_id = run_id
        self.claimed: dict[str, str] = {}

    def _claim(self):
        self.claimed.update(dict(self.runtime.claim_due(limit=100)))

    def process(self, node_id, branch_index=None):
        self._claim()
        rows = self.database._all(
            """SELECT * FROM vflow_points
               WHERE run_id = ? AND node_id = ?
                 AND status IN ('ready', 'claimed')
               ORDER BY visit_seq, id""",
            (self.run_id, node_id),
        )
        if branch_index is not None:
            rows = [
                row
                for row in rows
                if stack_of(row) and stack_of(row)[-1]["branch_index"] == branch_index
            ]
        assert rows, f"no processable point at {node_id!r}"
        row = rows[0]
        token = self.claimed.pop(row["id"], None)
        if token is None:
            self._claim()
            token = self.claimed.pop(row["id"], None)
        assert token is not None, f"point {row['id']!r} was not claimed"
        assert self.runtime.process_point(row["id"], token)


def test_nested_fork_merge_frames_and_ordered_outputs(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "nested", NESTED_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"seed": 1}
        )
        driver = Driver(runtime, database, run["id"])

        driver.process("splitA")
        outer = database._one(
            "SELECT * FROM vflow_fork_instances WHERE run_id = ? AND fork_node_id = 'splitA'",
            (run["id"],),
        )
        assert outer["status"] == "open"
        assert outer["expected_count"] == 2
        outer_frame = {
            "fork_instance_id": outer["id"],
            "fork_node_id": "splitA",
            "merge_node_id": "mergedA",
            "branch_index": 0,
            "expected_count": 2,
        }
        a0 = points_at(database, run["id"], "splitB")[0]
        a1 = points_at(database, run["id"], "other")[0]
        assert stack_of(a0) == [outer_frame]
        assert stack_of(a1) == [{**outer_frame, "branch_index": 1}]

        driver.process("splitB")
        inner = database._one(
            "SELECT * FROM vflow_fork_instances WHERE run_id = ? AND fork_node_id = 'splitB'",
            (run["id"],),
        )
        assert inner["status"] == "open"
        assert inner["merge_node_id"] == "mergedB"
        assert json.loads(inner["parent_fork_stack_json"]) == [outer_frame]
        inner_frame = {
            "fork_instance_id": inner["id"],
            "fork_node_id": "splitB",
            "merge_node_id": "mergedB",
            "branch_index": 0,
            "expected_count": 2,
        }
        b0 = points_at(database, run["id"], "left")[0]
        b1 = points_at(database, run["id"], "right")[0]
        assert stack_of(b0) == [outer_frame, inner_frame]
        assert stack_of(b1) == [outer_frame, {**inner_frame, "branch_index": 1}]

        # Arrive out of order: inner branch 1 first, then branch 0.
        driver.process("right")
        driver.process("mergedB")
        right_arrival = points_at(database, run["id"], "mergedB")[0]
        assert right_arrival["status"] == "waiting_merge"
        assert stack_of(right_arrival)[-1]["branch_index"] == 1
        assert (
            database._one(
                "SELECT status FROM vflow_fork_instances WHERE id = ?", (inner["id"],)
            )["status"]
            == "open"
        )

        driver.process("left")
        driver.process("mergedB")
        inner_after = database._one(
            "SELECT * FROM vflow_fork_instances WHERE id = ?", (inner["id"],)
        )
        assert inner_after["status"] == "merged"
        assert inner_after["continuation_point_id"] is not None
        continuations = [
            row
            for row in points_at(database, run["id"], "mergedA")
            if row["status"] == "ready"
        ]
        assert len(continuations) == 1
        continuation = continuations[0]
        assert continuation["id"] == inner_after["continuation_point_id"]
        assert stack_of(continuation) == [outer_frame]
        assert continuation["parent_point_id"] == b0["id"]

        # Arrive branch A1 first. Merge A must keep waiting for A0, and the
        # final output must still be ordered by branch index.
        driver.process("other")
        driver.process("mergedA", branch_index=1)
        assert runtime.get_run(run["id"])["status"] == "running"
        waiting = [
            row
            for row in points_at(database, run["id"], "mergedA")
            if row["status"] == "waiting_merge"
        ]
        assert len(waiting) == 1
        assert stack_of(waiting[0])[-1]["branch_index"] == 1
        assert (
            database._one(
                "SELECT status FROM vflow_fork_instances WHERE id = ?", (outer["id"],)
            )["status"]
            == "open"
        )

        driver.process("mergedA")

        finished = runtime.get_run(run["id"])
        assert finished["status"] == "succeeded", finished
        result = json.loads(finished["final_result_json"])
        assert result["source"] == {"seed": 1}
        assert [item["index"] for item in result["branches"]] == [0, 1]
        assert result["branches"][0]["data"] == {
            "source": {"seed": 1},
            "branches": [
                {"index": 0, "data": {"seed": 1, "side": "d"}},
                {"index": 1, "data": {"seed": 1, "side": "e"}},
            ],
        }
        assert result["branches"][1]["data"] == {"seed": 1, "side": "c"}
        assert (
            database._one(
                "SELECT status FROM vflow_fork_instances WHERE id = ?", (outer["id"],)
            )["status"]
            == "merged"
        )
    finally:
        database.close()


def test_if_outcomes_converge_at_inner_and_outer_merge(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(
            service, admin, "nested-if", NESTED_IF_SOURCE
        )
        runtime = VFlowRuntime(database)
        for payload, expected in (
            ({"inner": True, "outer": False}, True),
            ({"inner": False, "outer": True}, False),
        ):
            run = start_run(runtime, definition, version, admin, tmp_path, payload)
            finished = runtime.run_until_idle(run["id"])
            assert finished["status"] == "succeeded", finished
            result = json.loads(finished["final_result_json"])
            assert result["source"] == payload
            assert [item["index"] for item in result["branches"]] == [0, 1]
            assert result["branches"][0]["data"]["source"] == payload
            assert result["branches"][1]["data"] == payload
            outer = database._one(
                """SELECT * FROM vflow_fork_instances
                   WHERE run_id = ? AND fork_node_id = 'splitA'""",
                (run["id"],),
            )
            inner = database._one(
                """SELECT * FROM vflow_fork_instances
                   WHERE run_id = ? AND fork_node_id = 'splitB'""",
                (run["id"],),
            )
            assert outer["status"] == "merged"
            assert inner["status"] == "merged"
            inner_branches = result["branches"][0]["data"]["branches"]
            assert [item["index"] for item in inner_branches] == [0, 1]
            assert inner_branches[0]["data"]["inner"] is expected
            assert inner_branches[1]["data"]["inner"] is expected
    finally:
        database.close()


def test_cycle_terminates_with_if(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "cycle-if", CYCLE_IF_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"done": False}
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded", finished
        assert json.loads(finished["final_result_json"]) == {"done": True}
        assert finished["transition_count"] == 3
    finally:
        database.close()


def test_cycle_without_exit_hits_transition_limit(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(
            service, admin, "cycle-limit", CYCLE_LIMIT_SOURCE
        )
        runtime = VFlowRuntime(database, limits=VFlowLimits(max_transitions=5))
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"stop": False}
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "LIMIT_TRANSITIONS"
        assert finished["transition_count"] == 5
    finally:
        database.close()


def test_cycle_reentering_fork_creates_new_instance_per_iteration(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(
            service, admin, "cycle-fork", CYCLE_FORK_SOURCE
        )
        runtime = VFlowRuntime(database)
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"queue": [10, 20]}
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded", finished
        assert json.loads(finished["final_result_json"]) == {"queue": []}

        instances = database._all(
            """SELECT * FROM vflow_fork_instances
               WHERE run_id = ? AND fork_node_id = 'split'
               ORDER BY created_at, id""",
            (run["id"],),
        )
        assert len(instances) == 2
        assert [item["status"] for item in instances] == ["merged", "merged"]
        assert instances[0]["id"] != instances[1]["id"]
        assert (
            instances[0]["continuation_point_id"]
            != instances[1]["continuation_point_id"]
        )
        assert (
            instances[0]["parent_point_id"] != instances[1]["parent_point_id"]
        )
        assert (
            instances[0]["parent_visit_seq"] < instances[1]["parent_visit_seq"]
        )
        assert instances[0]["parent_fork_stack_json"] == "[]"
        assert instances[1]["parent_fork_stack_json"] == "[]"

        arrivals = database._all(
            """SELECT * FROM vflow_merge_arrivals
               WHERE fork_instance_id IN (?, ?)
               ORDER BY fork_instance_id, branch_index""",
            (instances[0]["id"], instances[1]["id"]),
        )
        assert len(arrivals) == 4
        assert [item["branch_index"] for item in arrivals] == [0, 1, 0, 1]
    finally:
        database.close()


@pytest.mark.parametrize(
    ("source", "expected_code"),
    (
        (BYPASS_END_SOURCE, "COMPILE_FORK_BYPASS_MERGE"),
        (IF_BYPASS_SOURCE, "COMPILE_FORK_BYPASS_MERGE"),
        (NESTED_CROSS_SOURCE, "COMPILE_MERGE_SCOPE_MISMATCH"),
        (UNPAIRED_MERGE_SOURCE, "COMPILE_MERGE_UNPAIRED"),
    ),
)
def test_compiler_rejects_scope_bypasses(source, expected_code):
    with pytest.raises(CompileError) as error:
        compile_source(source)
    codes = {item.code for item in error.value.diagnostics}
    assert expected_code in codes


def test_compiler_accepts_nested_fork_and_if_convergence():
    for source in (
        NESTED_SOURCE,
        NESTED_IF_SOURCE,
        CYCLE_IF_SOURCE,
        CYCLE_FORK_SOURCE,
    ):
        compiled = compile_source(source)
        assert compiled.graph.entry
        assert compiled.graph_hash


def insert_nested_recovery_state(database, run, point):
    """Simulate a crash with an open outer Fork and a complete inner Fork."""
    now = utcnow()
    outer_id = "outer-fork"
    inner_id = "inner-fork"
    outer_frame = {
        "fork_instance_id": outer_id,
        "fork_node_id": "splitA",
        "merge_node_id": "mergedA",
        "branch_index": 0,
        "expected_count": 2,
    }
    outer_frame_1 = {**outer_frame, "branch_index": 1}
    inner_frame = {
        "fork_instance_id": inner_id,
        "fork_node_id": "splitB",
        "merge_node_id": "mergedB",
        "branch_index": 0,
        "expected_count": 2,
    }
    inner_frame_1 = {**inner_frame, "branch_index": 1}
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET status = 'forked', updated_at = ? WHERE id = ?",
            (now, point["id"]),
        )
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, created_at)
               VALUES (?, ?, 'splitA', 'mergedA', ?, ?, 2, ?, '[]', 'open', ?)""",
            (
                outer_id,
                run["id"],
                point["id"],
                point["visit_seq"],
                json.dumps({"seed": 1}),
                now,
            ),
        )
        connection.execute(
            """INSERT INTO vflow_points
               (id, run_id, node_id, status, data_json, fork_stack_json,
                parent_point_id, visit_seq, attempt, created_at, updated_at)
               VALUES ('outer-branch-0', ?, 'splitB', 'forked', ?, ?, ?, 2, 1, ?, ?)""",
            (
                run["id"],
                json.dumps({"seed": 1}),
                json.dumps([outer_frame]),
                point["id"],
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO vflow_points
               (id, run_id, node_id, status, data_json, fork_stack_json,
                parent_point_id, visit_seq, attempt, created_at, updated_at)
               VALUES ('outer-arrival-1', ?, 'mergedA', 'waiting_merge', ?, ?, ?, 3, 1, ?, ?)""",
            (
                run["id"],
                json.dumps({"seed": 1, "side": "c"}),
                json.dumps([outer_frame_1]),
                point["id"],
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO vflow_merge_arrivals
               (id, fork_instance_id, merge_node_id, branch_index, point_id,
                data_json, arrived_at)
               VALUES ('outer-arrival-row-1', ?, 'mergedA', 1,
                       'outer-arrival-1', ?, ?)""",
            (outer_id, json.dumps({"seed": 1, "side": "c"}), now),
        )
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, created_at)
               VALUES (?, ?, 'splitB', 'mergedB', 'outer-branch-0', 2, 2, ?, ?, 'open', ?)""",
            (
                inner_id,
                run["id"],
                json.dumps({"seed": 1}),
                json.dumps([outer_frame]),
                now,
            ),
        )
        for branch_index, data in enumerate(
            ({"seed": 1, "side": "d"}, {"seed": 1, "side": "e"})
        ):
            point_id = f"inner-arrival-{branch_index}"
            frame = inner_frame if branch_index == 0 else inner_frame_1
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, 'mergedB', 'waiting_merge', ?, ?, 'outer-branch-0',
                           ?, 1, ?, ?)""",
                (
                    point_id,
                    run["id"],
                    json.dumps(data),
                    json.dumps([outer_frame, frame]),
                    4 + branch_index,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO vflow_merge_arrivals
                   (id, fork_instance_id, merge_node_id, branch_index, point_id,
                    data_json, arrived_at)
                   VALUES (?, ?, 'mergedB', ?, ?, ?, ?)""",
                (
                    f"inner-arrival-row-{branch_index}",
                    inner_id,
                    branch_index,
                    point_id,
                    json.dumps(data),
                    now,
                ),
            )
    return outer_id, inner_id, outer_frame


def test_nested_recovery_preserves_stack_without_duplicate_continuation(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "nested", NESTED_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(
            runtime, definition, version, admin, tmp_path, {"seed": 1}
        )
        point = database._one(
            "SELECT * FROM vflow_points WHERE run_id = ?", (run["id"],)
        )
        outer_id, inner_id, outer_frame = insert_nested_recovery_state(
            database, run, point
        )

        recovery = VFlowRecovery(database)
        first = recovery.run_once()
        assert first["recovered_merges"] == 1
        inner = database._one(
            "SELECT * FROM vflow_fork_instances WHERE id = ?", (inner_id,)
        )
        outer = database._one(
            "SELECT * FROM vflow_fork_instances WHERE id = ?", (outer_id,)
        )
        assert inner["status"] == "merged"
        assert inner["continuation_point_id"] is not None
        assert outer["status"] == "open"
        continuations = [
            row
            for row in points_at(database, run["id"], "mergedA")
            if row["status"] == "ready"
        ]
        assert len(continuations) == 1
        assert continuations[0]["id"] == inner["continuation_point_id"]
        assert stack_of(continuations[0]) == [outer_frame]

        points_before = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        second = recovery.run_once()
        points_after = database._one(
            "SELECT COUNT(*) AS count FROM vflow_points WHERE run_id = ?",
            (run["id"],),
        )["count"]
        assert second["recovered_merges"] == 0
        assert points_after == points_before

        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "succeeded", finished
        result = json.loads(finished["final_result_json"])
        assert [item["index"] for item in result["branches"]] == [0, 1]
        assert result["branches"][0]["data"]["branches"] == [
            {"index": 0, "data": {"seed": 1, "side": "d"}},
            {"index": 1, "data": {"seed": 1, "side": "e"}},
        ]
        assert result["branches"][1]["data"] == {"seed": 1, "side": "c"}
        outer_after = database._one(
            "SELECT * FROM vflow_fork_instances WHERE id = ?", (outer_id,)
        )
        assert outer_after["status"] == "merged"
        assert outer_after["continuation_point_id"] is None
        recovered_events = database._all(
            """SELECT * FROM vflow_events
               WHERE run_id = ? AND event_type = 'MERGE_RECOVERED'""",
            (run["id"],),
        )
        assert len(recovered_events) == 1
        assert json.loads(recovered_events[0]["payload_json"]) == {
            "fork_instance_id": inner_id,
            "count": 2,
        }
    finally:
        database.close()
