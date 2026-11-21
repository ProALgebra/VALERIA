from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opencode_web.app import create_app


@pytest.fixture
async def vflow_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        user_path = workspace / "worker"
        user_path.mkdir()
        db.create_user("worker", "a-strong-password", "user", str(user_path))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(app=app, client=client, db=db)


async def login(client, name="admin"):
    password = "correct-horse-battery-staple" if name == "admin" else "a-strong-password"
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return response.json()["user"]


async def preview_if(client, **body):
    return await client.post("/api/admin/vflow/preview-if", json=body)


async def test_preview_if_true_false_and_operators(vflow_app):
    client = vflow_app.client
    await login(client)

    true = await preview_if(
        client, input={"count": 3}, path="$.count", operator="gt", value=2
    )
    assert true.status_code == 200, true.text
    assert true.json() == {"result": True, "port": "true"}

    false = await preview_if(
        client, input={"count": 1}, path="$.count", operator="gt", value=2
    )
    assert false.status_code == 200, false.text
    assert false.json() == {"result": False, "port": "false"}

    typed = await preview_if(
        client, input={"count": "3"}, path="$.count", operator="eq", value=3
    )
    assert typed.status_code == 200, typed.text
    assert typed.json() == {"result": False, "port": "false"}

    contains = await preview_if(
        client, input={"tags": ["a", "b"]}, path="$.tags", operator="contains", value="b"
    )
    assert contains.status_code == 200, contains.text
    assert contains.json() == {"result": True, "port": "true"}

    in_list = await preview_if(
        client, input={"x": 1}, path="$.x", operator="in", value=[0, 1, 2]
    )
    assert in_list.status_code == 200, in_list.text
    assert in_list.json() == {"result": True, "port": "true"}


async def test_preview_if_missing_modes(vflow_app):
    client = vflow_app.client
    await login(client)

    default_error = await preview_if(
        client, input={}, path="$.absent", operator="eq", value=1
    )
    assert default_error.status_code == 400, default_error.text
    body = default_error.json()["error"]
    assert body["code"] == "VFLOW_BAD_REQUEST"
    assert "Missing path" in body["message"]
    assert body["request_id"]

    missing_false = await preview_if(
        client,
        input={},
        path="$.absent",
        operator="eq",
        value=1,
        missing="false",
    )
    assert missing_false.status_code == 200, missing_false.text
    assert missing_false.json() == {"result": False, "port": "false"}

    missing_true = await preview_if(
        client,
        input={},
        path="$.absent",
        operator="eq",
        value=1,
        missing="true",
    )
    assert missing_true.status_code == 200, missing_true.text
    assert missing_true.json() == {"result": True, "port": "true"}


async def test_preview_if_exists_and_is_null(vflow_app):
    client = vflow_app.client
    await login(client)
    input_data = {"present": 0, "empty": None}

    exists = await preview_if(
        client, input=input_data, path="$.present", operator="exists"
    )
    assert exists.status_code == 200, exists.text
    assert exists.json() == {"result": True, "port": "true"}

    absent = await preview_if(
        client, input=input_data, path="$.absent", operator="exists"
    )
    assert absent.status_code == 200, absent.text
    assert absent.json() == {"result": False, "port": "false"}

    is_null = await preview_if(
        client, input=input_data, path="$.empty", operator="is_null"
    )
    assert is_null.status_code == 200, is_null.text
    assert is_null.json() == {"result": True, "port": "true"}

    not_null = await preview_if(
        client, input=input_data, path="$.present", operator="is_null"
    )
    assert not_null.status_code == 200, not_null.text
    assert not_null.json() == {"result": False, "port": "false"}

    missing_null = await preview_if(
        client, input={}, path="$.absent", operator="is_null", missing="true"
    )
    assert missing_null.status_code == 200, missing_null.text
    assert missing_null.json() == {"result": False, "port": "false"}


async def test_preview_if_invalid_conditions(vflow_app):
    client = vflow_app.client
    await login(client)

    bad_operator = await preview_if(
        client, input={"x": 1}, path="$.x", operator="regex", value=".*"
    )
    assert bad_operator.status_code == 400, bad_operator.text
    assert bad_operator.json()["error"]["code"] == "VFLOW_BAD_REQUEST"

    bad_path = await preview_if(
        client, input={"x": 1}, path="x", operator="eq", value=1
    )
    assert bad_path.status_code == 400, bad_path.text
    assert "Path must start with '$'" in bad_path.json()["error"]["message"]

    bad_syntax = await preview_if(
        client, input={"x": 1}, path="$.x[*]", operator="eq", value=1
    )
    assert bad_syntax.status_code == 400, bad_syntax.text

    empty_path = await preview_if(
        client, input={"x": 1}, path="", operator="eq", value=1
    )
    assert empty_path.status_code == 400, empty_path.text
    assert empty_path.json()["error"]["message"] == "if requires a path"

    bad_missing = await preview_if(
        client,
        input={"x": 1},
        path="$.x",
        operator="eq",
        value=1,
        missing="sometimes",
    )
    assert bad_missing.status_code == 400, bad_missing.text
    assert "Invalid missing mode" in bad_missing.json()["error"]["message"]


async def test_preview_if_nested_input(vflow_app):
    client = vflow_app.client
    await login(client)
    input_data = {
        "order": {"items": [{"qty": 2}, {"qty": 5}]},
        "user": {"name": "Alpha"},
    }

    nested_index = await preview_if(
        client,
        input=input_data,
        path="$.order.items[1].qty",
        operator="ge",
        value=5,
    )
    assert nested_index.status_code == 200, nested_index.text
    assert nested_index.json() == {"result": True, "port": "true"}

    quoted_key = await preview_if(
        client,
        input=input_data,
        path="$['user']['name']",
        operator="contains",
        value="lph",
    )
    assert quoted_key.status_code == 200, quoted_key.text
    assert quoted_key.json() == {"result": True, "port": "true"}

    nested_in = await preview_if(
        client,
        input=input_data,
        path="$.order.items[0].qty",
        operator="in",
        value=[2, 3],
    )
    assert nested_in.status_code == 200, nested_in.text
    assert nested_in.json() == {"result": True, "port": "true"}


async def test_preview_if_admin_only_and_no_side_effects(vflow_app):
    client = vflow_app.client
    db = vflow_app.db
    await login(client, "worker")
    denied = await preview_if(
        client, input={"x": 1}, path="$.x", operator="eq", value=1
    )
    assert denied.status_code == 403, denied.text

    await login(client)
    before = db._one("SELECT COUNT(*) AS count FROM vflow_runs")["count"]
    allowed = await preview_if(
        client, input={"x": 1}, path="$.x", operator="eq", value=1
    )
    assert allowed.status_code == 200, allowed.text
    after = db._one("SELECT COUNT(*) AS count FROM vflow_runs")["count"]
    assert before == after == 0

    client.headers["x-csrf-token"] = ""
    no_csrf = await preview_if(
        client, input={"x": 1}, path="$.x", operator="eq", value=1
    )
    assert no_csrf.status_code == 403, no_csrf.text
