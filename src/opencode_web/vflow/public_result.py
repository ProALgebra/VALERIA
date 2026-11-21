"""Safe projection of a run result through an allowlist schema."""

from __future__ import annotations

from typing import Any


class PublicResultError(ValueError):
    """The public result cannot be produced safely."""


def project_public_result(value: Any, schema: Any) -> Any:
    """Return only the parts of ``value`` explicitly allowed by ``schema``."""
    if not isinstance(schema, dict) or not schema:
        raise PublicResultError("Public output schema is not configured")
    return _project(value, schema)


def _project(value: Any, schema: dict[str, Any]) -> Any:
    expected = schema.get("type")
    if expected == "object" or "properties" in schema:
        if not isinstance(value, dict):
            raise PublicResultError("Public result does not match object schema")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        result: dict[str, Any] = {}
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                result[key] = _project(value[key], child)
        return result
    if expected == "array":
        if not isinstance(value, list):
            raise PublicResultError("Public result does not match array schema")
        items = schema.get("items")
        if not isinstance(items, dict):
            return []
        return [_project(item, items) for item in value]
    if expected == "string" and not isinstance(value, str):
        raise PublicResultError("Public result does not match string schema")
    if expected == "boolean" and not isinstance(value, bool):
        raise PublicResultError("Public result does not match boolean schema")
    if expected == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise PublicResultError("Public result does not match integer schema")
    if expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise PublicResultError("Public result does not match number schema")
    if expected == "null" and value is not None:
        raise PublicResultError("Public result does not match null schema")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise PublicResultError("Public result is not an allowed enum value")
    return value
