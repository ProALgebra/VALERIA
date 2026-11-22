"""Safe JSON Schema subset validator for the JSON repair processor.

Only the keywords needed by deterministic repair are supported.  The module
never resolves references, never executes user code, and rejects schemas that
are too large or too deeply nested.  Diagnostic paths are stable and safe to
show in prompts or admin UI: ``$.field`` and ``$.items[0]``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import (
    SchemaDefinitionError,
    ValidationDiagnostic,
    ValidationResult,
    field_path,
    index_path,
    json_type_name,
    json_values_equal,
)

MAX_SCHEMA_BYTES = 16_384
MAX_SCHEMA_DEPTH = 16
MAX_ENUM_VALUES = 1_000
MAX_PATTERN_LENGTH = 1_024
MAX_DIAGNOSTICS = 100

SUPPORTED_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_ANNOTATIONS = frozenset({"$schema", "title", "description", "default", "examples"})
_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "additionalProperties",
    }
) | _ANNOTATIONS


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _definition_error(path: str, message: str) -> SchemaDefinitionError:
    return SchemaDefinitionError(message, path)


def validate_schema(schema: Any) -> None:
    """Reject malformed or unsupported schemas before repair starts."""

    if not isinstance(schema, dict):
        raise _definition_error("$", "schema must be a JSON object")
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _definition_error("$", "schema must be JSON-serializable") from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise _definition_error("$", f"schema exceeds {MAX_SCHEMA_BYTES} bytes")
    _validate_schema_node(schema, "$", 1)


def _validate_schema_node(schema: dict[str, Any], path: str, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise _definition_error(path, f"schema nesting exceeds {MAX_SCHEMA_DEPTH} levels")
    if "$ref" in schema:
        raise _definition_error(path, "external $ref is not supported")
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        keyword = sorted(unknown)[0]
        raise _definition_error(path, f"unsupported keyword {keyword!r}")

    expected = schema.get("type")
    if expected is not None and (
        not isinstance(expected, str) or expected not in SUPPORTED_TYPES
    ):
        raise _definition_error(path, "type must be a supported JSON type")

    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            raise _definition_error(path, "enum must be a non-empty array")
        if len(values) > MAX_ENUM_VALUES:
            raise _definition_error(path, "enum has too many values")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(
            not isinstance(item, str) or not item for item in required
        ):
            raise _definition_error(path, "required must be an array of non-empty strings")
        if len(set(required)) != len(required):
            raise _definition_error(path, "required contains duplicate fields")

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise _definition_error(path, "properties must be an object")
        for key, child in properties.items():
            if not isinstance(key, str):
                raise _definition_error(path, "property names must be strings")
            if not isinstance(child, dict):
                raise _definition_error(
                    field_path(path, key), "property schema must be an object"
                )
            _validate_schema_node(child, field_path(path, key), depth + 1)

    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            raise _definition_error(path, "items must be an object schema")
        _validate_schema_node(items, f"{path}[]", depth + 1)

    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if not isinstance(additional, bool):
            raise _definition_error(
                path, "additionalProperties must be a boolean"
            )

    for key in ("minimum", "maximum"):
        if key in schema and not _is_number(schema[key]):
            raise _definition_error(path, f"{key} must be a number")
    if (
        _is_number(schema.get("minimum"))
        and _is_number(schema.get("maximum"))
        and schema["minimum"] > schema["maximum"]
    ):
        raise _definition_error(path, "minimum cannot be greater than maximum")

    for key in ("minLength", "maxLength"):
        if key in schema and not _is_non_negative_int(schema[key]):
            raise _definition_error(path, f"{key} must be a non-negative integer")
    if (
        _is_non_negative_int(schema.get("minLength"))
        and _is_non_negative_int(schema.get("maxLength"))
        and schema["minLength"] > schema["maxLength"]
    ):
        raise _definition_error(path, "minLength cannot be greater than maxLength")

    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN_LENGTH:
            raise _definition_error(
                path, f"pattern must be a string of at most {MAX_PATTERN_LENGTH} characters"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _definition_error(path, "pattern is not a valid regular expression") from exc

    for key in ("$schema", "title", "description"):
        if key in schema and not isinstance(schema[key], str):
            raise _definition_error(path, f"{key} must be a string")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _diagnostic(
    diagnostics: list[ValidationDiagnostic],
    path: str,
    code: str,
    message: str,
) -> None:
    diagnostics.append(ValidationDiagnostic(code=code, message=message, path=path))


def _walk(
    value: Any,
    schema: dict[str, Any],
    path: str,
    diagnostics: list[ValidationDiagnostic],
    max_diagnostics: int,
) -> None:
    if len(diagnostics) >= max_diagnostics or not schema:
        return

    if "enum" in schema and not any(
        json_values_equal(value, choice) for choice in schema["enum"]
    ):
        _diagnostic(
            diagnostics,
            path,
            "ENUM_MISMATCH",
            "value is not one of the allowed enum values",
        )

    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        _diagnostic(
            diagnostics,
            path,
            "TYPE_MISMATCH",
            f"expected {expected}, got {json_type_name(value)}",
        )
        return

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                _diagnostic(
                    diagnostics,
                    field_path(path, key),
                    "REQUIRED_MISSING",
                    f"required property {key!r} is missing",
                )
        properties = schema.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                _walk(
                    value[key],
                    child,
                    field_path(path, key),
                    diagnostics,
                    max_diagnostics,
                )
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    _diagnostic(
                        diagnostics,
                        field_path(path, key),
                        "ADDITIONAL_PROPERTY",
                        "additional property is not allowed",
                    )

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for item_index, item in enumerate(value):
                _walk(
                    item,
                    items,
                    index_path(path, item_index),
                    diagnostics,
                    max_diagnostics,
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if _is_non_negative_int(minimum) and len(value) < minimum:
            _diagnostic(
                diagnostics,
                path,
                "STRING_TOO_SHORT",
                f"string is shorter than {minimum} characters",
            )
        if _is_non_negative_int(maximum) and len(value) > maximum:
            _diagnostic(
                diagnostics,
                path,
                "STRING_TOO_LONG",
                f"string is longer than {maximum} characters",
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            _diagnostic(
                diagnostics,
                path,
                "PATTERN_MISMATCH",
                "string does not match the required pattern",
            )

    if _is_number(value):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if _is_number(minimum) and value < minimum:
            _diagnostic(
                diagnostics,
                path,
                "NUMBER_TOO_SMALL",
                f"number is below the minimum {minimum}",
            )
        if _is_number(maximum) and value > maximum:
            _diagnostic(
                diagnostics,
                path,
                "NUMBER_TOO_LARGE",
                f"number is above the maximum {maximum}",
            )


def validate_value(
    value: Any,
    schema: Any = None,
    *,
    path: str = "$",
    max_diagnostics: int = MAX_DIAGNOSTICS,
) -> ValidationResult:
    """Validate a decoded value against the supported schema subset.

    The schema definition is revalidated here as a fail-closed guard, so callers
    cannot accidentally apply an unsupported or unsafe schema.
    """

    if schema is None:
        return ValidationResult(valid=True)
    if not isinstance(schema, dict):
        raise SchemaDefinitionError("schema must be a JSON object")
    validate_schema(schema)
    diagnostics: list[ValidationDiagnostic] = []
    _walk(value, schema, path or "$", diagnostics, max(1, max_diagnostics))
    return ValidationResult(valid=not diagnostics, diagnostics=tuple(diagnostics))


def is_valid(value: Any, schema: Any = None) -> bool:
    """Return whether ``value`` satisfies the supported schema subset."""

    return validate_value(value, schema).valid


__all__ = [
    "MAX_DIAGNOSTICS",
    "MAX_ENUM_VALUES",
    "MAX_PATTERN_LENGTH",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "SUPPORTED_TYPES",
    "is_valid",
    "validate_schema",
    "validate_value",
]
