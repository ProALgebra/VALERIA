"""Shared data structures for the standalone VALERIA JSON repair processor.

This module performs no I/O and has no dependency on VFlow, Task, database,
queue, or provider code.  Later phases build on these stable structures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ERROR_ENVELOPE_KEY = "valeria_json_processor"
ERROR_ENVELOPE_STATUS = "error"

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class RepairMode(StrEnum):
    """Supported repair modes.

    ``SYNTAX_ONLY`` may only change JSON syntax.  ``SCHEMA_SAFE`` additionally
    permits explicitly allow-listed deterministic transformations supplied by
    a later phase; it never permits free-form semantic rewriting.
    """

    SYNTAX_ONLY = "syntax_only"
    SCHEMA_SAFE = "schema_safe"


class AttemptPhase(StrEnum):
    """Where a repair attempt ran."""

    LOCAL = "local"
    LLM = "llm"


class RepairJobStatus(StrEnum):
    """Lifecycle of a persisted repair job.

    Only ``SUCCEEDED``, ``FAILED``, and ``CANCELLED`` are terminal.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> frozenset[RepairJobStatus]:
        return frozenset({cls.SUCCEEDED, cls.FAILED, cls.CANCELLED})

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal()


class RepairMethod(StrEnum):
    """How a repair result was produced."""

    NONE = "none"
    LOCAL = "local"
    LLM = "llm"


class RepairSourceType(StrEnum):
    """Where a repair request originated."""

    TASK = "task"
    VFLOW = "vflow"
    STANDALONE = "standalone"


class AttemptStatus(StrEnum):
    """Recorded outcome of one repair attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessorFailurePolicy(StrEnum):
    """What a caller does when repair fails.

    Only fail-closed behavior is supported: no ``null``/``{}`` substitution.
    """

    FAIL = "fail"


class ParseStrategy(StrEnum):
    """How a candidate JSON value was recovered from raw output."""

    DIRECT = "direct"
    NORMALIZED = "normalized"
    FENCED = "fenced"
    PROSE_EXTRACTED = "prose_extracted"
    FAILED = "failed"


class ProcessorErrorCode(StrEnum):
    """Stable error codes exposed by the processor.

    Provider, queue, timeout, and cancellation codes are reserved here so later
    phases do not need to redesign the error contract.
    """

    INVALID_INPUT = "INVALID_INPUT"
    NOT_REPAIRABLE = "NOT_REPAIRABLE"
    AMBIGUOUS_JSON = "AMBIGUOUS_JSON"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    RAW_OUTPUT_TOO_LARGE = "RAW_OUTPUT_TOO_LARGE"
    SCHEMA_DEFINITION_INVALID = "SCHEMA_DEFINITION_INVALID"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    # Alias kept for callers that use the shorter M5 wording.  The persisted
    # value remains the stable M1 code.
    SCHEMA_INVALID = "SCHEMA_VALIDATION_FAILED"
    CONTENT_CHANGED = "CONTENT_CHANGED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_ENABLED = "NOT_ENABLED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_STATE_CONFLICT = "JOB_STATE_CONFLICT"
    LEASE_LOST = "LEASE_LOST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    SETTINGS_INVALID = "SETTINGS_INVALID"
    SETTINGS_VERSION_CONFLICT = "SETTINGS_VERSION_CONFLICT"


@dataclass(frozen=True, slots=True)
class ValidationDiagnostic:
    """One safe, user-displayable validation problem."""

    code: str
    message: str
    path: str = "$"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


class SchemaDefinitionError(ValueError):
    """The configured JSON Schema is outside the supported safe subset."""

    def __init__(self, message: str, path: str = "$") -> None:
        self.message = message
        self.path = path
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True, slots=True)
class ProcessorError(Exception):
    """A fail-closed processor error.

    ``to_envelope()`` returns a shape that cannot be confused with repaired
    task JSON: it is always rooted at ``ERROR_ENVELOPE_KEY`` and carries an
    explicit ``status: "error"`` marker.
    """

    code: ProcessorErrorCode
    message: str
    path: str | None = None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": ERROR_ENVELOPE_STATUS,
            "code": self.code.value,
            "message": self.message,
        }
        if self.path is not None:
            body["path"] = self.path
        if self.diagnostics:
            body["diagnostics"] = [item.to_dict() for item in self.diagnostics]
        return {ERROR_ENVELOPE_KEY: body}

    def to_envelope(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of validating one parsed value against the safe schema subset."""

    valid: bool
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def to_error(self) -> ProcessorError | None:
        if self.valid:
            return None
        first = self.diagnostics[0] if self.diagnostics else None
        return ProcessorError(
            ProcessorErrorCode.SCHEMA_VALIDATION_FAILED,
            first.message if first is not None else "JSON does not satisfy the schema",
            first.path if first is not None else "$",
            self.diagnostics,
        )


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Outcome of parsing raw model output into one JSON value."""

    ok: bool
    value: Any = None
    raw_output: str = ""
    strategy: ParseStrategy = ParseStrategy.FAILED
    normalizations: tuple[str, ...] = ()
    candidate_count: int = 0
    error: ProcessorError | None = None

    @classmethod
    def success(
        cls,
        value: Any,
        *,
        raw_output: str,
        strategy: ParseStrategy,
        normalizations: tuple[str, ...] = (),
        candidate_count: int = 1,
    ) -> ParseResult:
        return cls(
            ok=True,
            value=value,
            raw_output=raw_output,
            strategy=strategy,
            normalizations=normalizations,
            candidate_count=candidate_count,
        )

    @classmethod
    def failure(
        cls,
        error: ProcessorError,
        *,
        raw_output: str,
        candidate_count: int = 0,
    ) -> ParseResult:
        return cls(
            ok=False,
            raw_output=raw_output,
            candidate_count=candidate_count,
            error=error,
        )

    def to_envelope(self) -> dict[str, Any] | None:
        return None if self.error is None else self.error.to_envelope()


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One local or LLM repair attempt, retained for audit/history."""

    number: int
    phase: AttemptPhase
    mode: RepairMode
    raw_output: str
    parse: ParseResult
    validation: ValidationResult | None = None
    error: ProcessorError | None = None

    @property
    def ok(self) -> bool:
        if self.error is not None or not self.parse.ok:
            return False
        return self.validation is None or self.validation.valid


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Aggregate result of a repair pipeline.

    ``attempts`` is the ordered history.  Both the original raw output and the
    final value are retained so later phases can persist them and so failures
    never silently become downstream task data.
    """

    ok: bool
    mode: RepairMode
    value: Any = None
    raw_output: str = ""
    attempts: tuple[RepairAttempt, ...] = ()
    error: ProcessorError | None = None

    @property
    def history(self) -> tuple[RepairAttempt, ...]:
        return self.attempts

    @property
    def method(self) -> RepairMethod:
        """How this result was produced (``none``/``local``/``llm``)."""

        if any(item.phase is AttemptPhase.LLM for item in self.attempts):
            return RepairMethod.LLM
        if any(item.phase is AttemptPhase.LOCAL for item in self.attempts):
            return RepairMethod.LOCAL
        return RepairMethod.NONE

    def to_envelope(self) -> dict[str, Any] | None:
        return None if self.error is None else self.error.to_envelope()


def field_path(base: str, key: str) -> str:
    """Build a stable diagnostic path for an object field."""

    if _IDENTIFIER_RE.match(key):
        return f"{base}.{key}"
    escaped = key.replace("\\", "\\\\").replace("'", "\\'")
    return f"{base}['{escaped}']"


def index_path(base: str, index: int) -> str:
    """Build a stable diagnostic path for an array item."""

    return f"{base}[{index}]"


def json_type_name(value: Any) -> str:
    """Return the JSON type name of a decoded Python value."""

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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def json_values_equal(left: Any, right: Any) -> bool:
    """Compare decoded JSON values without conflating booleans and numbers."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_values_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def is_error_envelope(value: Any) -> bool:
    """Return whether ``value`` has the reserved processor error shape."""

    if not isinstance(value, dict) or set(value) != {ERROR_ENVELOPE_KEY}:
        return False
    body = value[ERROR_ENVELOPE_KEY]
    return (
        isinstance(body, dict)
        and body.get("status") == ERROR_ENVELOPE_STATUS
        and isinstance(body.get("code"), str)
        and isinstance(body.get("message"), str)
    )


__all__ = [
    "ERROR_ENVELOPE_KEY",
    "ERROR_ENVELOPE_STATUS",
    "AttemptPhase",
    "AttemptStatus",
    "ParseResult",
    "ParseStrategy",
    "ProcessorError",
    "ProcessorErrorCode",
    "ProcessorFailurePolicy",
    "RepairAttempt",
    "RepairJobStatus",
    "RepairMethod",
    "RepairMode",
    "RepairResult",
    "RepairSourceType",
    "SchemaDefinitionError",
    "ValidationDiagnostic",
    "ValidationResult",
    "field_path",
    "index_path",
    "is_error_envelope",
    "json_type_name",
    "json_values_equal",
]
