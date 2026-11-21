from __future__ import annotations

import pytest

from opencode_web.vflow.logic import (
    MISSING,
    LogicError,
    apply_transform,
    evaluate_if,
    get_path,
    parse_path,
)


def test_parse_limited_paths():
    assert parse_path("$") == ()
    assert parse_path("$.a.b") == ("a", "b")
    assert parse_path("$['a-b'][0]") == ("a-b", 0)
    assert get_path({"a": {"b": [10]}}, "$.a.b[0]") == 10
    assert get_path({}, "$.missing") is MISSING
    with pytest.raises(LogicError):
        parse_path("$.*")
    with pytest.raises(LogicError):
        parse_path("$.a[*]")


def test_transform_copy_select_merge_append_and_rename():
    data = {
        "task": {"output": {"json": {"repository": "core"}}},
        "context": {},
        "items": [1],
    }
    result = apply_transform(
        data,
        {
            "operations": [
                {
                    "op": "copy",
                    "from": "$.task.output.json.repository",
                    "to": "$.context.repository",
                },
                {"op": "merge_object", "path": "$.context", "object": {"ok": True}},
                {"op": "append", "path": "$.items", "value": 2},
                {"op": "rename", "from": "$.context.ok", "to": "$.context.valid"},
                {
                    "op": "set",
                    "path": "$.context.source",
                    "value": {"from_path": "$.task.output.json.repository"},
                },
            ]
        },
    )
    assert result["context"] == {
        "repository": "core",
        "valid": True,
        "source": "core",
    }
    assert result["items"] == [1, 2]
    assert data["context"] == {}


def test_transform_missing_path_is_deterministic():
    with pytest.raises(LogicError):
        apply_transform({}, {"operations": [{"op": "copy", "from": "$.x", "to": "$.y"}]})
    assert apply_transform(
        {}, {"operations": [{"op": "set", "path": "$.x", "value": 1}], "create_missing": True}
    ) == {"x": 1}


def test_if_typed_comparisons_and_missing():
    assert evaluate_if({"n": 3}, {"path": "$.n", "operator": "gt", "value": 2})
    assert not evaluate_if({"n": "3"}, {"path": "$.n", "operator": "gt", "value": 2})
    assert evaluate_if({"ok": True}, {"path": "$.ok", "operator": "eq", "value": True})
    assert not evaluate_if({}, {"path": "$.ok", "operator": "exists"})
    assert evaluate_if({}, {"path": "$.ok", "operator": "exists", "missing": "true"}) is False
    assert evaluate_if({}, {"path": "$.ok", "operator": "eq", "value": True, "missing": "true"})
    with pytest.raises(LogicError):
        evaluate_if({}, {"path": "$.ok", "operator": "eq", "value": True})


def test_if_contains_and_in():
    assert evaluate_if({"tags": ["a", "b"]}, {
        "path": "$.tags", "operator": "contains", "value": "a"
    })
    assert evaluate_if({"name": "alpha"}, {
        "path": "$.name", "operator": "contains", "value": "ph"
    })
    assert evaluate_if({"x": 1}, {
        "path": "$.x", "operator": "in", "value": [0, 1, 2]
    })
    assert not evaluate_if({"x": True}, {
        "path": "$.x", "operator": "in", "value": [1]
    })
