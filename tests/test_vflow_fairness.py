from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from opencode_web.database import Database, utcnow
from opencode_web.vflow.runtime import DEFAULT_PER_RUN_CLAIM_LIMIT, VFlowRuntime
from opencode_web.vflow.service import VFlowService

PASS_SOURCE = """\
workflow "pass-through" {
  version: 1;
  input: json;
  step pass = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> pass -> end
}
"""


def wide_fork_source(branches: int) -> str:
    steps = "\n".join(
        f'  step branch{index} = transform(operations: [\n'
        f'    {{op: "set", path: "$.branch{index}", value: {index}}}\n'
        f"  ]);"
        for index in range(branches)
    )
    edges = "\n".join(
        f"  split[{index}] -> branch{index} -> combined;"
        for index in range(branches)
    )
    return f"""\
workflow "wide-fork" {{
  version: 1;
  input: json;
  step split = fork(count: {branches}, join: combined);
{steps}
  step combined = merge();
  start -> split;
{edges}
  combined -> end
}}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "fairness.sqlite3", "secret")
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


def start_run(runtime, definition, version, admin, tmp_path):
    return runtime.create_run(
        definition_id=definition["id"],
        version_id=version["id"],
        user_id=admin["id"],
        workspace_id=str(tmp_path),
        input_data={},
    )


def seed_ready_points(database, run_id, count, created_at=None):
    """Replace a new Run's entry point with ``count`` directly ready points."""
    due_at = created_at or utcnow()
    point_ids: set[str] = set()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vflow_points SET status = 'consumed', updated_at = ? WHERE run_id = ?",
            (due_at, run_id),
        )
        for index in range(count):
            point_id = str(uuid.uuid4())
            point_ids.add(point_id)
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, 'bulk', 'ready', '{}', '[]', ?, 1, ?, ?)""",
                (point_id, run_id, 100 + index, due_at, due_at),
            )
    return point_ids


def counts_by_run(database, claimed):
    counts: dict[str, int] = {}
    for point_id, _token in claimed:
        row = database._one("SELECT run_id FROM vflow_points WHERE id = ?", (point_id,))
        assert row is not None
        counts[row["run_id"]] = counts.get(row["run_id"], 0) + 1
    return counts


def test_claim_round_robins_caps_wide_run_and_serves_other_run(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "fair-pass", PASS_SOURCE)
        runtime = VFlowRuntime(database)
        base = datetime.now(UTC)
        wide_run = start_run(runtime, definition, version, admin, tmp_path)
        wide_ids = seed_ready_points(
            database,
            wide_run["id"],
            12,
            created_at=base.isoformat(),
        )
        other_run = start_run(runtime, definition, version, admin, tmp_path)
        other_ids = seed_ready_points(
            database,
            other_run["id"],
            2,
            created_at=(base + timedelta(seconds=1)).isoformat(),
        )

        claimed = runtime.claim_due(limit=20)
        claimed_ids = [point_id for point_id, _token in claimed]
        counts = counts_by_run(database, claimed)

        # The wide Run has the earliest points and reaches its burst cap; the
        # other Run still gets every ready point instead of being starved.
        assert len(claimed) == DEFAULT_PER_RUN_CLAIM_LIMIT + 2
        assert counts == {wide_run["id"]: DEFAULT_PER_RUN_CLAIM_LIMIT, other_run["id"]: 2}
        assert claimed_ids[0] in wide_ids
        assert claimed_ids[1] in other_ids
        assert sum(point_id in wide_ids for point_id in claimed_ids) == 4
        assert sum(point_id in other_ids for point_id in claimed_ids) == 2
    finally:
        database.close()


def test_claim_gives_two_wide_runs_equal_burst_shares(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "fair-share", PASS_SOURCE)
        runtime = VFlowRuntime(database)
        first = start_run(runtime, definition, version, admin, tmp_path)
        seed_ready_points(database, first["id"], 10)
        second = start_run(runtime, definition, version, admin, tmp_path)
        seed_ready_points(database, second["id"], 10)

        claimed = runtime.claim_due(limit=20)
        counts = counts_by_run(database, claimed)

        assert len(claimed) == DEFAULT_PER_RUN_CLAIM_LIMIT * 2
        assert counts == {
            first["id"]: DEFAULT_PER_RUN_CLAIM_LIMIT,
            second["id"]: DEFAULT_PER_RUN_CLAIM_LIMIT,
        }
    finally:
        database.close()


def test_claim_per_run_cap_is_configurable(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "fair-config", PASS_SOURCE)
        runtime = VFlowRuntime(database, per_run_claim_limit=2)
        base = datetime.now(UTC)
        wide_run = start_run(runtime, definition, version, admin, tmp_path)
        wide_ids = seed_ready_points(
            database,
            wide_run["id"],
            5,
            created_at=base.isoformat(),
        )
        other_run = start_run(runtime, definition, version, admin, tmp_path)
        other_ids = seed_ready_points(
            database,
            other_run["id"],
            1,
            created_at=(base + timedelta(seconds=1)).isoformat(),
        )

        claimed = runtime.claim_due(limit=20)
        counts = counts_by_run(database, claimed)

        assert len(claimed) == 3
        assert counts == {wide_run["id"]: 2, other_run["id"]: 1}
        assert [point_id in wide_ids for point_id, _ in claimed] == [True, False, True]
        assert claimed[1][0] in other_ids
    finally:
        database.close()


def test_repeated_claims_do_not_duplicate_leased_points(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(service, admin, "fair-lease", PASS_SOURCE)
        runtime = VFlowRuntime(database)
        run = start_run(runtime, definition, version, admin, tmp_path)
        seeded = seed_ready_points(database, run["id"], 10)

        first = runtime.claim_due(limit=20)
        second = runtime.claim_due(limit=20)
        third = runtime.claim_due(limit=20)
        fourth = runtime.claim_due(limit=20)

        first_ids = {point_id for point_id, _ in first}
        second_ids = {point_id for point_id, _ in second}
        third_ids = {point_id for point_id, _ in third}
        assert [len(first), len(second), len(third)] == [4, 4, 2]
        assert first_ids.isdisjoint(second_ids)
        assert first_ids.isdisjoint(third_ids)
        assert second_ids.isdisjoint(third_ids)
        assert first_ids | second_ids | third_ids == seeded
        assert fourth == []

        for point_id, token in [*first, *second, *third]:
            row = database._one("SELECT * FROM vflow_points WHERE id = ?", (point_id,))
            assert row is not None
            assert row["status"] == "claimed"
            assert row["lease_token"] == token
    finally:
        database.close()


def test_wide_single_run_still_completes_across_ticks(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        branches = 9
        definition, version = publish(
            service,
            admin,
            "fair-wide-fork",
            wide_fork_source(branches),
        )
        runtime = VFlowRuntime(database)
        run = start_run(runtime, definition, version, admin, tmp_path)

        finished = runtime.run_until_idle(run["id"])

        assert finished["status"] == "succeeded", finished
        result = json.loads(finished["final_result_json"])
        assert [branch["index"] for branch in result["branches"]] == list(
            range(branches)
        )
    finally:
        database.close()
