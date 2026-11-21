from __future__ import annotations

import pytest

from opencode_web.vflow.public_result import (
    PublicResultError,
    project_public_result,
)


def test_projection_allowlists_nested_and_array_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                },
            },
        },
    }
    value = {
        "ok": True,
        "secret": "internal",
        "items": [{"title": "a", "secret": 1}, {"title": "b"}],
    }
    assert project_public_result(value, schema) == {
        "ok": True,
        "items": [{"title": "a"}, {"title": "b"}],
    }


def test_projection_rejects_missing_schema_and_bad_types() -> None:
    with pytest.raises(PublicResultError):
        project_public_result({"ok": True}, None)
    with pytest.raises(PublicResultError):
        project_public_result(
            {"ok": "yes"},
            {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        )
