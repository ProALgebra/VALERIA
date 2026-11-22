from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.vflow.gateway import ValeriaTaskGateway


@pytest.fixture
async def vflow_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        users = {}
        for name, only in (("worker", False), ("tasker", True), ("stranger", False)):
            path = workspace / name
            path.mkdir()
            users[name] = db.create_user(
                name, "a-strong-password", "user", str(path), tasks_only=only
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(app=app, client=client, db=db, users=users)


async def login(client, name="admin"):
    password = "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return response.json()["user"]


async def create_task(client):
    await login(client)
    response = await client.post(
        "/api/admin/tasks",
        json={
            "title": "VFlow smoke task",
            "description": "Used by the VFlow API test",
            "content": "Do the thing",
            "agent": "build",
            "model": "provider/coder",
            "model_variant": "high",
            "is_published": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def source_for(task_id: str) -> str:
    return f'''workflow "review" {{
  version: 1;
  input: json;
  step work = task.run(task_id: "{task_id}", input_path: "$");
  start -> work -> end
}}'''


async def test_pipeline_publication_and_user_catalog(vflow_app):
    t = vflow_app
    await login(t.client)
    task = await create_task(t.client)

    created = await t.client.post(
        "/api/admin/pipelines",
        json={
            "slug": "review-flow",
            "title": "Review flow",
            "description": "A visible pipeline",
        },
    )
    assert created.status_code == 201, created.text
    pipeline = created.json()
    assert pipeline["status"] == "active"

    draft_response = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json={"source": source_for(task["id"]), "input_schema": {"type": "object"}},
    )
    assert draft_response.status_code == 200, draft_response.text
    draft = draft_response.json()
    assert draft["status"] == "draft"
    assert draft["etag"].startswith('"')

    missing_etag = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json={"source": source_for(task["id"])},
    )
    assert missing_etag.status_code == 412
    assert missing_etag.json()["error"]["code"] == "VFLOW_PRECONDITION_FAILED"

    validation = await t.client.post(
        f"/api/admin/pipelines/{pipeline['id']}/validate"
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True

    published = await t.client.post(
        f"/api/admin/pipelines/{pipeline['id']}/publish",
        headers={"If-Match": draft["etag"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    catalog = await t.client.get("/api/pipelines")
    assert catalog.status_code == 200, catalog.text
    items = catalog.json()
    assert [item["slug"] for item in items] == ["review-flow"]
    item = items[0]
    assert item["version"] == 1
    assert item["public_result_enabled"] is False
    assert "graph" not in item
    assert "source" not in item
    assert task["id"] not in catalog.text

    detail = await t.client.get("/api/pipelines/review-flow")
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == "Review flow"
    assert "graph" not in detail.json()

    admin_detail = await t.client.get(f"/api/admin/pipelines/{pipeline['id']}")
    assert admin_detail.status_code == 200, admin_detail.text
    versions = admin_detail.json()["versions"]
    first_version = next(v for v in versions if v["version_number"] == 1)

    second = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json={"source": source_for(task["id"])},
    )
    assert second.status_code == 200, second.text
    second_publish = await t.client.post(
        f"/api/admin/pipelines/{pipeline['id']}/publish"
    )
    assert second_publish.status_code == 200, second_publish.text
    assert (await t.client.get("/api/pipelines")).json()[0]["version"] == 2

    rollback = await t.client.post(
        f"/api/admin/pipelines/{pipeline['id']}/rollback",
        json={"version_id": first_version["id"]},
    )
    assert rollback.status_code == 200, rollback.text
    assert (await t.client.get("/api/pipelines")).json()[0]["version"] == 1


async def test_audience_archive_roles_and_tasks_only(vflow_app):
    t = vflow_app
    await login(t.client)
    task = await create_task(t.client)
    pipeline = (
        await t.client.post(
            "/api/admin/pipelines",
            json={"slug": "restricted-flow", "title": "Restricted flow"},
        )
    ).json()
    assert (
        await t.client.put(
            f"/api/admin/pipelines/{pipeline['id']}/draft",
            json={"source": source_for(task["id"])},
        )
    ).status_code == 200
    assert (
        await t.client.post(f"/api/admin/pipelines/{pipeline['id']}/publish")
    ).status_code == 200

    worker_id = t.users["worker"]["id"]
    audience = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/audience",
        json={"mode": "selected", "user_ids": [worker_id]},
    )
    assert audience.status_code == 200, audience.text
    assert (await t.client.get("/api/pipelines")).json() == []

    await login(t.client, "worker")
    assert [p["slug"] for p in (await t.client.get("/api/pipelines")).json()] == [
        "restricted-flow"
    ]
    assert (await t.client.get("/api/admin/pipelines")).status_code == 403

    await login(t.client, "stranger")
    assert (await t.client.get("/api/pipelines")).json() == []
    assert (await t.client.get("/api/pipelines/restricted-flow")).status_code == 404

    await login(t.client, "tasker")
    assert (await t.client.get("/api/pipelines")).status_code == 200
    assert (await t.client.get("/api/pipelines")).json() == []

    await login(t.client)
    archived = await t.client.post(f"/api/admin/pipelines/{pipeline['id']}/archive")
    assert archived.status_code == 200, archived.text
    await login(t.client, "worker")
    assert (await t.client.get("/api/pipelines")).json() == []


async def test_compile_and_preview_transform(vflow_app):
    t = vflow_app
    await login(t.client)
    source = """\
workflow "preview" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""
    compiled = await t.client.post(
        "/api/admin/vflow/compile", json={"source": source}
    )
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["graph"]["workflow"] == "preview"
    assert compiled.json()["graph_hash"]

    preview = await t.client.post(
        "/api/admin/vflow/preview-transform",
        json={
            "input": {"a": 1},
            "operations": [{"op": "set", "path": "$.b", "value": 2}],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["result"] == {"a": 1, "b": 2}

    invalid = await t.client.post(
        "/api/admin/vflow/compile",
        json={"source": 'workflow "broken" { step x = nope(); start -> end }'},
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "VFLOW_VALIDATION_ERROR"


async def test_task_json_schema_and_repair_limits_save_from_inspector(vflow_app):
    t = vflow_app
    await login(t.client)
    task = await create_task(t.client)
    created = await t.client.post(
        "/api/admin/pipelines",
        json={"slug": "task-json", "title": "Task JSON"},
    )
    assert created.status_code == 201, created.text
    pipeline = created.json()
    source = f'''workflow "task-json" {{
  version: 1;
  input: json;
  step work = task.run(
    task_id: "{task['id']}",
    result_mode: "strict_json",
    json_schema: {{
      type: "object",
      required: ["ok"],
      properties: {{ok: {{type: "boolean"}}}},
      additionalProperties: false
    }},
    max_json_repairs: 2
  );
  start -> work -> end
}}'''
    saved = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        json={"source": source},
    )
    assert saved.status_code == 200, saved.text
    node = next(
        item for item in saved.json()["graph"]["nodes"] if item["type"] == "valeria.task"
    )
    assert node["config"]["max_json_repairs"] == 2
    assert node["config"]["json_schema"]["additionalProperties"] is False

    invalid_schema = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        headers={"If-Match": saved.json()["etag"]},
        json={"source": source.replace('"boolean"', '"not-a-type"')},
    )
    assert invalid_schema.status_code == 422, invalid_schema.text
    codes = {
        item["code"]
        for item in invalid_schema.json()["error"]["details"]["diagnostics"]
    }
    assert "COMPILE_INVALID_JSON_SCHEMA" in codes

    invalid_repairs = await t.client.put(
        f"/api/admin/pipelines/{pipeline['id']}/draft",
        headers={"If-Match": saved.json()["etag"]},
        json={"source": source.replace("max_json_repairs: 2", "max_json_repairs: 4")},
    )
    assert invalid_repairs.status_code == 422, invalid_repairs.text
    codes = {
        item["code"]
        for item in invalid_repairs.json()["error"]["details"]["diagnostics"]
    }
    assert "COMPILE_INVALID_MAX_JSON_REPAIRS" in codes


async def test_user_queue_and_job_api_hide_vflow_repair_jobs(vflow_app):
    t = vflow_app
    await login(t.client)
    task = await create_task(t.client)
    worker = t.users["worker"]
    gateway = ValeriaTaskGateway(t.db)
    original_id = gateway.enqueue(
        run_id="run-repair-api",
        point_id="point-repair-api",
        task_id=task["id"],
        user_id=worker["id"],
        workspace_id=worker["workspace_path"],
        input_data={},
        result_mode="strict_json",
        node_id="work",
    )
    t.db.complete_task_run(original_id, {"message_id": "m-1", "text": "not json"})
    repair_id = gateway.repair_output(
        task_run_id=original_id,
        run_id="run-repair-api",
        point_id="point-repair-api",
        repair_attempt=1,
        prompt="Return only valid JSON",
    )

    await login(t.client, "worker")
    queue = await t.client.get("/api/queue/me")
    assert queue.status_code == 200, queue.text
    assert all(item["id"] != repair_id for item in queue.json())
    hidden = await t.client.get(f"/api/jobs/{repair_id}")
    assert hidden.status_code == 404, hidden.text
    cannot_cancel = await t.client.delete(f"/api/jobs/{repair_id}")
    assert cannot_cancel.status_code == 404, cannot_cancel.text
    assert t.db.get_job(repair_id)["status"] == "queued"

    await login(t.client)
    visible = await t.client.get(f"/api/jobs/{repair_id}")
    assert visible.status_code == 200, visible.text
    assert visible.json()["job_kind"] == "vflow_repair"
