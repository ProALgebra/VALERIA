from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database
from opencode_web.vflow.repository import VFlowBadRequest
from opencode_web.vflow.runtime import VFlowRuntime, VFlowRuntimeError
from opencode_web.vflow.service import VFlowService

SEQ_SOURCE = """\
workflow "policy-review" {
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
workflow "policy-fork" {
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

API_SOURCE = """\
workflow "policy-api" {
  version: 1;
  input: json;
  step ok = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> ok -> end
}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "policy.sqlite3", "secret")
    database.initialize()
    admin = database.create_user(
        "admin", "a-strong-password", "admin", str(tmp_path)
    )
    return database, admin, VFlowService(database)


def publish(service, admin, slug, source, *, settings=None):
    definition = service.create_definition(
        admin["id"], slug=slug, title=slug, settings=settings
    )
    service.save_draft(definition["id"], admin["id"], source=source)
    service.publish(definition["id"], admin["id"])
    version = service.repository.get_published(definition["id"])
    assert version is not None
    return definition, version


def test_settings_validation_rejects_looser_and_unknown_values(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition = service.create_definition(
            admin["id"], slug="policy-settings", title="Policy settings"
        )
        with pytest.raises(VFlowBadRequest, match="global limit"):
            service.set_settings(
                definition["id"],
                admin["id"],
                {"limits": {"max_fork_n": 999}},
            )
        with pytest.raises(VFlowBadRequest, match="Unknown policy setting"):
            service.set_settings(
                definition["id"], admin["id"], {"unknown": True}
            )
        with pytest.raises(VFlowBadRequest, match="Unknown limit"):
            service.set_settings(
                definition["id"],
                admin["id"],
                {"limits": {"unknown": 1}},
            )
        with pytest.raises(VFlowBadRequest, match="positive integer"):
            service.set_settings(
                definition["id"],
                admin["id"],
                {"limits": {"max_json_bytes": 0}},
            )
        with pytest.raises(VFlowBadRequest, match="cancel_own"):
            service.set_settings(
                definition["id"], admin["id"], {"cancel_own": "no"}
            )

        updated = service.set_settings(
            definition["id"],
            admin["id"],
            {"limits": {"max_fork_n": 2}, "cancel_own": False},
        )
        assert updated["settings"] == {
            "cancel_own": False,
            "limits": {"max_fork_n": 2},
        }
        audit = database._all(
            """SELECT action, metadata_json FROM vflow_audit_events
               WHERE action = 'definition.settings' ORDER BY id DESC LIMIT 1"""
        )
        assert audit[0]["action"] == "definition.settings"
        assert json.loads(audit[0]["metadata_json"])["cancel_own"] is False
    finally:
        database.close()


def test_run_pins_effective_limits_when_settings_change(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition, version = publish(
            service,
            admin,
            "pinned-limits",
            SEQ_SOURCE,
            settings={"limits": {"max_transitions": 100}},
        )
        runtime = VFlowRuntime(database)
        run_a = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        assert json.loads(run_a["limits_json"])["max_transitions"] == 100

        service.set_settings(
            definition["id"], admin["id"], {"limits": {"max_transitions": 1}}
        )
        finished_a = runtime.run_until_idle(run_a["id"])
        assert finished_a["status"] == "succeeded", finished_a
        assert json.loads(finished_a["limits_json"])["max_transitions"] == 100

        run_b = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        assert json.loads(run_b["limits_json"])["max_transitions"] == 1
        service.set_settings(
            definition["id"], admin["id"], {"limits": {"max_transitions": 100}}
        )
        finished_b = runtime.run_until_idle(run_b["id"])
        assert finished_b["status"] == "failed", finished_b
        assert finished_b["error_code"] == "LIMIT_TRANSITIONS"
        assert json.loads(finished_b["limits_json"])["max_transitions"] == 1
    finally:
        database.close()


def test_definition_json_and_fork_limits_are_enforced_per_run(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        restricted, restricted_version = publish(
            service,
            admin,
            "json-limit",
            API_SOURCE,
            settings={"limits": {"max_json_bytes": 10}},
        )
        runtime = VFlowRuntime(database)
        with pytest.raises(VFlowRuntimeError) as exc:
            runtime.create_run(
                definition_id=restricted["id"],
                version_id=restricted_version["id"],
                user_id=admin["id"],
                workspace_id=str(tmp_path),
                input_data={"long": "value"},
            )
        assert exc.value.code == "LIMIT_JSON_BYTES"

        forked, forked_version = publish(
            service,
            admin,
            "fork-limit",
            FORK_SOURCE,
            settings={"limits": {"max_fork_n": 1}},
        )
        run = runtime.create_run(
            definition_id=forked["id"],
            version_id=forked_version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )
        finished = runtime.run_until_idle(run["id"])
        assert finished["status"] == "failed", finished
        assert finished["error_code"] == "LIMIT_FORK_COUNT"
    finally:
        database.close()


@pytest.fixture
async def policy_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        users = {}
        for name in ("worker", "other"):
            path = workspace / name
            path.mkdir()
            users[name] = db.create_user(
                name, "a-strong-password", "user", str(path)
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(app=app, client=client, db=db, users=users)


async def login(client, name: str = "admin") -> None:
    password = (
        "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    )
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


async def publish_api_pipeline(client, *, settings=None) -> dict:
    await login(client)
    created = await client.post(
        "/api/admin/pipelines",
        json={"slug": "policy-api", "title": "Policy API", "settings": settings},
    )
    assert created.status_code == 201, created.text
    pipeline = created.json()
    draft = await client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json={"source": API_SOURCE},
    )
    assert draft.status_code == 200, draft.text
    published = await client.post(
        f"/api/admin/pipelines/{pipeline['id']}/publish",
        headers={"If-Match": draft.json()["etag"]},
    )
    assert published.status_code == 200, published.text
    return pipeline


async def test_settings_endpoint_validates_and_affects_new_runs(policy_app):
    t = policy_app
    pipeline = await publish_api_pipeline(t.client)
    updated = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/settings",
        json={
            "settings": {
                "limits": {"max_json_bytes": 10},
                "cancel_own": False,
            }
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["settings"] == {
        "limits": {"max_json_bytes": 10},
        "cancel_own": False,
    }

    looser = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/settings",
        json={"settings": {"limits": {"max_json_bytes": 9_999_999}}},
    )
    assert looser.status_code == 400, looser.text
    assert looser.json()["error"]["code"] == "VFLOW_BAD_REQUEST"
    assert "global limit" in looser.json()["error"]["message"]

    unknown = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/settings",
        json={"settings": {"surprise": 1}},
    )
    assert unknown.status_code == 400, unknown.text

    await login(t.client, "worker")
    rejected = await t.client.post(
        "/api/pipelines/policy-api/runs", json={"input": {"long": "value"}}
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "LIMIT_JSON_BYTES"
    count = t.db._one("SELECT COUNT(*) AS count FROM vflow_runs")["count"]
    assert count == 0


async def test_cancel_policy_user_denied_and_admin_bypass(policy_app):
    t = policy_app
    pipeline = await publish_api_pipeline(t.client)
    disabled = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/settings",
        json={"settings": {"cancel_own": False}},
    )
    assert disabled.status_code == 200, disabled.text

    await login(t.client, "worker")
    created = await t.client.post(
        "/api/pipelines/policy-api/runs", json={"input": {}}
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    denied = await t.client.post(f"/api/pipeline-runs/{run_id}/cancel")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "VFLOW_CANCEL_FORBIDDEN"
    assert t.app.state.vflow_runtime.get_run(run_id)["status"] == "running"

    await login(t.client, "other")
    assert (
        await t.client.post(f"/api/pipeline-runs/{run_id}/cancel")
    ).status_code == 404

    await login(t.client)
    allowed = await t.client.post(f"/api/admin/vflow/runs/{run_id}/cancel")
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "cancelled"

    enabled = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/settings",
        json={"settings": {"cancel_own": True}},
    )
    assert enabled.status_code == 200, enabled.text
    await login(t.client, "worker")
    second = await t.client.post(
        "/api/pipelines/policy-api/runs", json={"input": {}}
    )
    assert second.status_code == 202, second.text
    cancelled = await t.client.post(
        f"/api/pipeline-runs/{second.json()['id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
