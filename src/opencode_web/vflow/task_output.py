"""Task JSON output contract shared by the pure handler and the runtime.

The processor path is fail-closed: only a decoded JSON value can enter
``output.json``.  A malformed or schema-invalid model answer is never replaced
with ``null``, ``{}`` or an error envelope inside the Task output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..json_processor.models import SchemaDefinitionError
from ..json_processor.schema import validate_value as validate_processor_value

OUTPUT_MODES = {"text", "json"}
ON_JSON_ERROR_MODES = {"fail", "repair"}
REPAIR_TRIGGERS = {"invalid_json", "invalid_json_or_schema"}


@dataclass(frozen=True)
class JsonOutputCheck:
    """Outcome of a strict JSON parse plus optional schema validation."""

    ok: bool
    value: Any = None
    error_code: str = ""
    message: str = ""
    path: str = "$"


def output_mode(config: dict[str, Any]) -> str:
    value = config.get("output_mode", "text")
    return value if value in OUTPUT_MODES else "text"


def on_json_error(config: dict[str, Any]) -> str:
    value = config.get("on_json_error", "fail")
    return value if value in ON_JSON_ERROR_MODES else "fail"


def repair_trigger(config: dict[str, Any]) -> str:
    value = config.get("repair_trigger", "invalid_json")
    return value if value in REPAIR_TRIGGERS else "invalid_json"


def effective_output_schema(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the new schema, falling back to the legacy ``json_schema``."""

    if "output_schema" in config:
        schema = config.get("output_schema")
        return schema if isinstance(schema, dict) else None
    schema = config.get("json_schema")
    return schema if isinstance(schema, dict) and schema else None


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON constant {value!r}")


def check_json_output(text: Any, schema: Any = None) -> JsonOutputCheck:
    """Strictly parse the whole final text and validate it against ``schema``."""

    if not isinstance(text, str):
        return JsonOutputCheck(
            ok=False,
            error_code="OUTPUT_INVALID",
            message="Task output text is unavailable",
        )
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (TypeError, ValueError):
        return JsonOutputCheck(
            ok=False,
            error_code="OUTPUT_INVALID",
            message="Task output is not valid JSON",
        )
    if schema is None:
        return JsonOutputCheck(ok=True, value=value)
    try:
        validation = validate_processor_value(value, schema)
    except SchemaDefinitionError as exc:
        return JsonOutputCheck(
            ok=False,
            error_code="OUTPUT_CONFIG_INVALID",
            message=f"Task output schema is invalid: {exc}",
            path=getattr(exc, "path", "$"),
        )
    if not validation.valid:
        first = validation.diagnostics[0] if validation.diagnostics else None
        return JsonOutputCheck(
            ok=False,
            error_code="OUTPUT_INVALID",
            message=(
                first.message
                if first is not None
                else "Task output does not satisfy the JSON schema"
            ),
            path=first.path if first is not None else "$",
        )
    return JsonOutputCheck(ok=True, value=value)


def build_task_output(
    *,
    input_data: Any,
    task_id: str,
    task_run_id: str | None,
    text: str,
    value: Any,
) -> dict[str, Any]:
    """Build the backward-compatible envelope plus the new ``output`` alias."""

    output = {"text": text, "json": value}
    return {
        "input": input_data,
        "output": output,
        "task": {
            "task_id": task_id,
            "run_id": task_run_id,
            "status": "completed",
            "output": output,
        },
    }


__all__ = [
    "JsonOutputCheck",
    "ON_JSON_ERROR_MODES",
    "OUTPUT_MODES",
    "REPAIR_TRIGGERS",
    "build_task_output",
    "check_json_output",
    "effective_output_schema",
    "on_json_error",
    "output_mode",
    "repair_trigger",
]
