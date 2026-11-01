"""Keep the exposed administrator surface aligned with the application's API."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from admin_operations import OPERATIONS


def test_registry_covers_admin_routes_and_matches_actual_http_statuses():
    root = Path(__file__).resolve().parents[3]
    routes = {}
    for filename in ("app.py", "consultations.py"):
        tree = ast.parse((root / "src/opencode_web" / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    continue
                route = decorator.args[0].value
                if not isinstance(route, str) or not route.startswith("/api/"):
                    continue
                status = next(
                    (ast.literal_eval(keyword.value) for keyword in decorator.keywords
                     if keyword.arg == "status_code"), 200,
                )
                routes[(decorator.func.attr.upper(), route)] = status
    covered = set()
    for operation in OPERATIONS.values():
        key = (operation.method, "/" + operation.path)
        assert key in routes, operation.name
        assert operation.expected_status == routes[key], operation.name
        covered.add(key)
        assert operation.input_model.model_json_schema()["additionalProperties"] is False
    admin_routes = {key for key in routes if key[1].startswith("/api/admin/")}
    assert admin_routes <= covered


@pytest.mark.parametrize("suffix", ["update_task", "update_template"])
def test_patch_keeps_omission_distinct_from_clearing(suffix):
    id_name = "task_id" if suffix == "update_task" else "template_id"
    operation = OPERATIONS[f"valeria_admin_{suffix}"]
    assert operation.prepare({id_name: "id", "title": "Changed"})["body"] == {"title": "Changed"}
    assert operation.prepare({id_name: "id", "description": None})["body"] == {
        "description": None,
    }
    for field in ("title", "content", "is_published"):
        with pytest.raises(ValidationError):
            operation.prepare({id_name: "id", field: None})


def test_pipeline_memberships_limits_queries_and_password_metadata():
    task = OPERATIONS["valeria_admin_update_task"]
    assert "next_task_ids" not in task.prepare({"task_id": "task", "title": "Title"})["body"]
    assert task.prepare({"task_id": "task", "next_task_ids": []})["body"] == {"next_task_ids": []}
    user = OPERATIONS["valeria_admin_update_user"]
    assert "max_active_jobs" not in user.prepare({"user_id": "user"})["body"]
    assert user.prepare({"user_id": "user", "max_active_jobs": None})["body"] == {
        "max_active_jobs": None,
    }
    query = OPERATIONS["valeria_admin_get_statistics"].prepare({
        "day": "2026-11-01", "timezone": "Europe/Belgrade",
    })
    assert query["params"] == {"day": "2026-11-01", "timezone": "Europe/Belgrade"}
    assert query["body"] is None
    for suffix in ("create_user", "reset_user_password"):
        schema = OPERATIONS[f"valeria_admin_{suffix}"].input_model.model_json_schema()
        assert schema["properties"]["password"]["writeOnly"] is True


@pytest.mark.parametrize("identifier", ["../admin", "foo/bar", "foo?x=1", "foo\n", "foo%2fbar"])
def test_path_arguments_cannot_change_fixed_routes(identifier):
    with pytest.raises(ValidationError):
        OPERATIONS["valeria_admin_get_job"].prepare({"job_id": identifier})
