from __future__ import annotations

import pytest

from opencode_web.vflow.api import _validate_input
from opencode_web.vflow.repository import VFlowBadRequest


def test_accepts_matching_input():
    schema = {
        "type": "object",
        "required": ["name", "count"],
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "count": {"type": "integer", "minimum": 1},
        },
    }
    _validate_input(schema, {"name": "ab", "count": 3})


def test_rejects_enum_mismatch():
    schema = {"type": "string", "enum": ["a", "b"]}
    with pytest.raises(VFlowBadRequest, match="allowed values"):
        _validate_input(schema, "c")


def test_rejects_string_length_and_pattern():
    with pytest.raises(VFlowBadRequest, match="too short"):
        _validate_input({"type": "string", "minLength": 3}, "ab")
    with pytest.raises(VFlowBadRequest, match="too long"):
        _validate_input({"type": "string", "maxLength": 2}, "abc")
    with pytest.raises(VFlowBadRequest, match="pattern"):
        _validate_input({"type": "string", "pattern": "^[a-z]+$"}, "ABC")


def test_rejects_numeric_bounds():
    with pytest.raises(VFlowBadRequest, match="below the minimum"):
        _validate_input({"type": "number", "minimum": 10}, 9)
    with pytest.raises(VFlowBadRequest, match="above the maximum"):
        _validate_input({"type": "integer", "maximum": 5}, 6)


def test_rejects_array_bounds_and_items():
    with pytest.raises(VFlowBadRequest, match="too few items"):
        _validate_input({"type": "array", "minItems": 2}, [1])
    with pytest.raises(VFlowBadRequest, match="too many items"):
        _validate_input({"type": "array", "maxItems": 1}, [1, 2])
    with pytest.raises(VFlowBadRequest, match="string"):
        _validate_input(
            {"type": "array", "items": {"type": "string"}},
            ["ok", 2],
        )


def test_rejects_missing_required_field():
    schema = {"type": "object", "required": ["name"]}
    with pytest.raises(VFlowBadRequest, match="Missing required"):
        _validate_input(schema, {})
