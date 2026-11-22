"""Small, safe JSON Schema subset used by VFlow Task output validation.

This module never executes user code.  It validates the schema definition at
compile time and validates already-parsed JSON values at runtime.  Error paths
are stable and safe to show to a user or an agent: ``$.field`` and
``$.items[0]``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_SCHEMA_BYTES = 16_384
MAX_SCHEMA_DEPTH = 16
MAX_ENUM_VALUES = 1_000
MAX_PATTERN_LENGTH = 1_024
MAX_PROMPT_SCHEMA_CHARS = 4_000
MAX_PROMPT_EXAMPLE_CHARS = 1_000

SUPPORTED_TYPES = {
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
}
_STRING_ANNOTATIONS = {"title", "description", "$schema"}
_ANNOTATIONS = {"title", "description", "$schema", "default", "examples"}
_SUPPORTED_KEYWORDS = {
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
    "minItems",
    "maxItems",
} | _ANNOTATIONS


class SchemaDefinitionError(ValueError):
    """The configured schema is malformed or outside the supported subset."""


@dataclass(frozen=True)
class JsonOutputError(Exception):
    """A safe validation failure for one Task final answer."""

    code: str
    message: str
    path: str = "$"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}

    def __str__(self) -> str:
        return self.message


def _definition_error(path: str, message: str) -> SchemaDefinitionError:
    return SchemaDefinitionError(f"{path}: {message}")


def _field_path(base: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{base}.{key}"
    escaped = key.replace("\\", "\\\\").replace("'", "\\'")
    return f"{base}['{escaped}']"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integral(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_schema(schema: Any) -> None:
    """Reject malformed/unsupported schemas before they enter a graph version."""
    if not isinstance(schema, dict):
        raise _definition_error("$", "schema must be a JSON object")
    try:
        size = len(
            json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise _definition_error("$", "schema must be JSON-serializable") from exc
    if size > MAX_SCHEMA_BYTES:
        raise _definition_error("$", "schema is too large")
    _validate_schema_node(schema, "$", 1)


def _validate_schema_node(schema: dict[str, Any], path: str, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise _definition_error(path, "schema nesting is too deep")
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
            raise _definition_error(path, "required must be an array of strings")
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
                    _field_path(path, key), "property schema must be an object"
                )
            _validate_schema_node(child, _field_path(path, key), depth + 1)
    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            raise _definition_error(path, "items must be an object schema")
        _validate_schema_node(items, f"{path}[]", depth + 1)
    additional = schema.get("additionalProperties")
    if additional is not None:
        if isinstance(additional, dict):
            _validate_schema_node(additional, f"{path}.*", depth + 1)
        elif not isinstance(additional, bool):
            raise _definition_error(
                path, "additionalProperties must be a boolean or object schema"
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
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (
            not _is_integral(schema[key]) or schema[key] < 0
        ):
            raise _definition_error(path, f"{key} must be a non-negative integer")
    if (
        _is_integral(schema.get("minLength"))
        and _is_integral(schema.get("maxLength"))
        and schema["minLength"] > schema["maxLength"]
    ):
        raise _definition_error(path, "minLength cannot be greater than maxLength")
    if (
        _is_integral(schema.get("minItems"))
        and _is_integral(schema.get("maxItems"))
        and schema["minItems"] > schema["maxItems"]
    ):
        raise _definition_error(path, "minItems cannot be greater than maxItems")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN_LENGTH:
            raise _definition_error(path, "pattern must be a short string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _definition_error(path, "pattern is not a valid regular expression") from exc
    for key in _STRING_ANNOTATIONS:
        if key in schema and not isinstance(schema[key], str):
            raise _definition_error(path, f"{key} must be a string")


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality keeps booleans distinct from numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _fail(path: str, message: str, code: str = "TASK_SCHEMA_INVALID") -> JsonOutputError:
    return JsonOutputError(code, f"{path}: {message}", path)


def validate_value(value: Any, schema: Any, path: str = "$") -> None:
    """Validate a parsed JSON value against the supported schema subset."""
    if not isinstance(schema, dict) or not schema:
        return
    path = path or "$"
    if "enum" in schema and not any(
        _json_equal(value, choice) for choice in schema["enum"]
    ):
        raise _fail(path, "value is not one of the allowed values")
    expected = schema.get("type")
    if isinstance(expected, str):
        matches = (
            expected == "object"
            and isinstance(value, dict)
            or expected == "array"
            and isinstance(value, list)
            or expected == "string"
            and isinstance(value, str)
            or expected == "integer"
            and _is_integral(value)
            or expected == "number"
            and _is_number(value)
            or expected == "boolean"
            and isinstance(value, bool)
            or expected == "null"
            and value is None
        )
        if not matches:
            raise _fail(path, f"expected {expected}, got {_type_name(value)}")

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                raise _fail(
                    _field_path(path, key),
                    f"required property {key!r} is missing",
                )
        properties = schema.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                validate_value(value[key], child, _field_path(path, key))
        additional = schema.get("additionalProperties")
        if additional is False:
            for key in value:
                if key not in properties:
                    raise _fail(
                        _field_path(path, key),
                        "additional property is not allowed",
                    )
        elif isinstance(additional, dict):
            for key in value:
                if key not in properties:
                    validate_value(value[key], additional, _field_path(path, key))

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if _is_integral(minimum) and len(value) < minimum:
            raise _fail(path, f"array has fewer than {minimum} items")
        if _is_integral(maximum) and len(value) > maximum:
            raise _fail(path, f"array has more than {maximum} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                validate_value(item, items, f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if _is_integral(minimum) and len(value) < minimum:
            raise _fail(path, f"string is shorter than {minimum} characters")
        if _is_integral(maximum) and len(value) > maximum:
            raise _fail(path, f"string is longer than {maximum} characters")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = False
            if not matched:
                raise _fail(path, "string does not match the required pattern")

    if _is_number(value):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if _is_number(minimum) and value < minimum:
            raise _fail(path, f"number is below the minimum {minimum}")
        if _is_number(maximum) and value > maximum:
            raise _fail(path, f"number is above the maximum {maximum}")


def validate_output(
    text: Any, schema: Any = None
) -> tuple[Any, JsonOutputError | None]:
    """Parse an entire Task answer and validate it against an optional schema."""
    if not isinstance(text, str):
        return None, JsonOutputError(
            "TASK_OUTPUT_NOT_JSON",
            "Task output is not valid JSON",
            "$",
        )
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        position = (
            f" at line {line} column {column}"
            if isinstance(line, int) and isinstance(column, int)
            else ""
        )
        return None, JsonOutputError(
            "TASK_OUTPUT_NOT_JSON",
            f"Task output is not valid JSON{position}",
            "$",
        )
    if schema:
        try:
            validate_value(parsed, schema)
        except JsonOutputError as exc:
            return parsed, exc
    return parsed, None


def _example(value_schema: Any, depth: int = 0) -> Any:
    if depth > MAX_SCHEMA_DEPTH or not isinstance(value_schema, dict):
        return None
    if "default" in value_schema:
        return value_schema["default"]
    choices = value_schema.get("enum")
    if isinstance(choices, list) and choices:
        return choices[0]
    expected = value_schema.get("type")
    if expected == "object":
        properties = value_schema.get("properties") or {}
        required = value_schema.get("required") or []
        keys = required or list(properties)
        return {
            key: _example(properties.get(key, {}), depth + 1)
            for key in keys
        }
    if expected == "array":
        count = value_schema.get("minItems")
        count = count if _is_integral(count) else 0
        item = _example(value_schema.get("items") or {}, depth + 1)
        return [item for _ in range(min(max(count, 0), 3))]
    if expected == "string":
        minimum = value_schema.get("minLength")
        length = minimum if _is_integral(minimum) else 0
        return "string" + ("x" * max(0, min(length - len("string"), 200)))
    if expected == "integer":
        minimum = value_schema.get("minimum")
        return int(minimum) if _is_integral(minimum) else 0
    if expected == "number":
        minimum = value_schema.get("minimum")
        return minimum if _is_number(minimum) else 0
    if expected == "boolean":
        return True
    if expected == "null":
        return None
    return None


def schema_example(schema: Any) -> Any:
    """Return a small illustrative value; never treated as a substitute for validation."""
    return _example(schema)


def build_correction_prompt(failure: JsonOutputError, schema: Any) -> str:
    """Build one bounded same-chat JSON repair instruction."""
    if failure.code == "TASK_SCHEMA_INVALID":
        reason = (
            "The previous answer did not match the required JSON schema: "
            f"{failure.message[:1_000]}"
        )
    else:
        reason = f"The previous answer was not valid JSON: {failure.message[:1_000]}"
    lines = [
        "Your previous answer was rejected by automatic JSON validation.",
        reason,
    ]
    if schema:
        try:
            schema_text = json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        except (TypeError, ValueError):
            schema_text = "{}"
        lines.extend(
            [
                "Required JSON Schema:",
                schema_text[:MAX_PROMPT_SCHEMA_CHARS],
                "Example of a valid answer:",
                json.dumps(
                    schema_example(schema),
                    ensure_ascii=False,
                    sort_keys=True,
                )[:MAX_PROMPT_EXAMPLE_CHARS],
            ]
        )
    lines.append(
        "Return only valid JSON that satisfies the schema. "
        "Do not wrap it in markdown code fences and do not add explanations."
    )
    return "\n".join(lines)


__all__ = [
    "JsonOutputError",
    "SchemaDefinitionError",
    "build_correction_prompt",
    "schema_example",
    "validate_output",
    "validate_schema",
    "validate_value",
]
