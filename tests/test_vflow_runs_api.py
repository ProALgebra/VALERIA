from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app

SOURCE = """\
workflow "run-flow" {
  version: 1;
  input: json;
  step ok = transform(operations: [
    {op: "set", path: "$.ok", value: true},
    {op: "set", path: "$.secret", value: "internal"}
  ]);
  start -> ok -> end
}
"""


@pytest.fixture
async def run_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        users = {}
        for name, only in (("worker", False), ("tasker", True), ("other", False)):
            path = workspace / name
            path.mkdir()
            users[name] = db.create_user(
                name, "a-strong-password", "user", str(path), tasks_only=only
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(app=app, client=client, db=db, users=users)


async def login(client, name: str) -> None:
    password = (
        "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    )
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


async def publish_pipeline(
    client,
    *,
    slug: str = "run-flow",
    public_output_schema: dict | None = None,
) -> dict:
    await login(client, "admin")
    created = await client.post(
        "/api/admin/pipelines",
        json={"slug": slug, "title": "Run flow"},
    )
    assert created.status_code == 201, created.text
    pipeline = created.json()
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }
    draft_body = {"source": SOURCE, "input_schema": schema}
    if public_output_schema is not None:
        draft_body["public_output_schema"] = public_output_schema
    draft = await client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json=draft_body,
    )
    assert draft.status_code == 200, draft.text
    published = await client.post(
        f"/api/admin/pipelines/{pipeline['id']}/publish",
        headers={"If-Match": draft.json()["etag"]},
    )
    assert published.status_code == 200, published.text
    return pipeline


async def test_run_launch_idempotency_progress_and_owner_isolation(run_app):
    t = run_app
    await publish_pipeline(t.client)
    await login(t.client, "worker")

    response = await t.client.post(
        "/api/pipelines/run-flow/runs",
        headers={"Idempotency-Key": "launch-1"},
        json={"input": {"name": "Alice"}},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "running"
    assert run["pipeline"]["slug"] == "run-flow"
    assert "workspace_id" not in run
    assert "input" not in run

    repeated = await t.client.post(
        "/api/pipelines/run-flow/runs",
        headers={"Idempotency-Key": "launch-1"},
        json={"input": {"name": "Alice"}},
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["id"] == run["id"]

    conflict = await t.client.post(
        "/api/pipelines/run-flow/runs",
        headers={"Idempotency-Key": "launch-1"},
        json={"input": {"name": "Bob"}},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    t.app.state.vflow_runtime.run_until_idle(run["id"])
    detail = await t.client.get(f"/api/pipeline-runs/{run['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "succeeded"
    assert body["progress"]["points_total"] == 1
    assert body["progress"]["points_done"] == 1

    listing = await t.client.get("/api/pipeline-runs")
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()] == [run["id"]]

    await login(t.client, "other")
    assert (await t.client.get(f"/api/pipeline-runs/{run['id']}")).status_code == 404
    assert (await t.client.get("/api/pipeline-runs")).json() == []


async def test_input_validation_cancel_and_tasks_only(run_app):
    t = run_app
    await publish_pipeline(t.client)
    await login(t.client, "worker")

    invalid = await t.client.post(
        "/api/pipelines/run-flow/runs",
        json={"input": {}},
    )
    assert invalid.status_code == 400, invalid.text
    assert "Missing required input field" in invalid.json()["error"]["message"]

    created = await t.client.post(
        "/api/pipelines/run-flow/runs",
        json={"input": {"name": "Alice"}},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    cancelled = await t.client.post(f"/api/pipeline-runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    await login(t.client, "tasker")
    assert (await t.client.get("/api/pipelines")).status_code == 200
    allowed = await t.client.post(
        "/api/pipelines/run-flow/runs",
        json={"input": {"name": "Tasker"}},
    )
    assert allowed.status_code == 202, allowed.text


async def test_admin_run_diagnostics(run_app):
    t = run_app
    pipeline = await publish_pipeline(t.client)
    await login(t.client, "worker")
    created = await t.client.post(
        "/api/pipelines/run-flow/runs",
        json={"input": {"name": "Alice"}},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    assert (await t.client.get("/api/admin/vflow/runs")).status_code == 403

    await login(t.client, "admin")
    listing = await t.client.get(
        "/api/admin/vflow/runs", params={"definition_id": pipeline["id"]}
    )
    assert listing.status_code == 200, listing.text
    item = listing.json()[0]
    assert item["id"] == run_id
    assert item["started_by"]["login"] == "worker"
    assert "points" not in item

    detail = await t.client.get(f"/api/admin/vflow/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["points"][0]["node_id"] == "ok"
    assert body["points"][0]["status"] == "ready"
    assert body["events"][0]["event_type"] == "RUN_STARTED"

    cancelled = await t.client.post(f"/api/admin/vflow/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_public_result_projection_and_access(run_app) -> None:
    await publish_pipeline(
        run_app.client,
        public_output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    await login(run_app.client, "worker")
    created = await run_app.client.post(
        "/api/pipelines/run-flow/runs",
        json={"input": {"name": "x"}},
        headers={"Idempotency-Key": "result-1"},
    )
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["public_result_enabled"] is True
    finished = run_app.app.state.vflow_runtime.run_until_idle(run["id"])
    assert finished["status"] == "succeeded"

    result = await run_app.client.get(f"/api/pipeline-runs/{run['id']}/result")
    assert result.status_code == 200, result.text
    assert result.json() == {"result": {"ok": True}}
    assert "secret" not in result.text

    await login(run_app.client, "other")
    hidden = await run_app.client.get(f"/api/pipeline-runs/{run['id']}/result")
    assert hidden.status_code == 404

    await login(run_app.client, "admin")
    await publish_pipeline(run_app.client, slug="no-result")
    await login(run_app.client, "worker")
    plain = await run_app.client.post(
        "/api/pipelines/no-result/runs",
        json={"input": {"name": "x"}},
        headers={"Idempotency-Key": "result-2"},
    )
    assert plain.status_code == 202, plain.text
    plain_run = plain.json()
    assert plain_run["public_result_enabled"] is False
    run_app.app.state.vflow_runtime.run_until_idle(plain_run["id"])
    disabled = await run_app.client.get(
        f"/api/pipeline-runs/{plain_run['id']}/result"
    )
    assert disabled.status_code == 404


async def test_run_list_filters_pagination_and_validation(run_app) -> None:
    t = run_app
    first = await publish_pipeline(t.client, slug="filter-first")
    await publish_pipeline(t.client, slug="filter-second")
    await login(t.client, "worker")

    succeeded = await t.client.post(
        "/api/pipelines/filter-second/runs",
        json={"input": {"name": "succeeded"}},
    )
    assert succeeded.status_code == 202, succeeded.text
    succeeded_id = succeeded.json()["id"]
    finished = t.app.state.vflow_runtime.run_until_idle(succeeded_id)
    assert finished["status"] == "succeeded"

    running = await t.client.post(
        "/api/pipelines/filter-first/runs",
        json={"input": {"name": "running"}},
    )
    assert running.status_code == 202, running.text
    running_id = running.json()["id"]

    cancelled = await t.client.post(
        "/api/pipelines/filter-first/runs",
        json={"input": {"name": "cancelled"}},
    )
    assert cancelled.status_code == 202, cancelled.text
    cancelled_id = cancelled.json()["id"]
    cancel_response = await t.client.post(f"/api/pipeline-runs/{cancelled_id}/cancel")
    assert cancel_response.status_code == 200, cancel_response.text

    all_runs = await t.client.get("/api/pipeline-runs")
    assert all_runs.status_code == 200, all_runs.text
    assert [item["id"] for item in all_runs.json()] == [
        cancelled_id,
        running_id,
        succeeded_id,
    ]

    by_status = await t.client.get(
        "/api/pipeline-runs", params={"status": "succeeded"}
    )
    assert by_status.status_code == 200, by_status.text
    assert [item["id"] for item in by_status.json()] == [succeeded_id]

    by_pipeline = await t.client.get(
        "/api/pipeline-runs", params={"pipeline_id": first["id"]}
    )
    assert by_pipeline.status_code == 200, by_pipeline.text
    assert [item["id"] for item in by_pipeline.json()] == [
        cancelled_id,
        running_id,
    ]

    combined = await t.client.get(
        "/api/pipeline-runs",
        params={"status": "running", "pipeline_id": first["id"]},
    )
    assert combined.status_code == 200, combined.text
    assert [item["id"] for item in combined.json()] == [running_id]

    page = await t.client.get(
        "/api/pipeline-runs", params={"limit": 1, "offset": 1}
    )
    assert page.status_code == 200, page.text
    assert [item["id"] for item in page.json()] == [running_id]

    last_page = await t.client.get(
        "/api/pipeline-runs", params={"limit": 2, "offset": 2}
    )
    assert last_page.status_code == 200, last_page.text
    assert [item["id"] for item in last_page.json()] == [succeeded_id]

    beyond = await t.client.get("/api/pipeline-runs", params={"offset": 50})
    assert beyond.status_code == 200, beyond.text
    assert beyond.json() == []

    invalid = await t.client.get(
        "/api/pipeline-runs", params={"status": "unknown"}
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "VFLOW_BAD_REQUEST"

    invalid_offset = await t.client.get(
        "/api/pipeline-runs", params={"offset": -1}
    )
    assert invalid_offset.status_code == 400, invalid_offset.text

    await login(t.client, "other")
    other_status = await t.client.get(
        "/api/pipeline-runs", params={"status": "succeeded"}
    )
    assert other_status.json() == []
    other_pipeline = await t.client.get(
        "/api/pipeline-runs", params={"pipeline_id": first["id"]}
    )
    assert other_pipeline.json() == []
