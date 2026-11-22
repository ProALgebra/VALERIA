"""Standalone VALERIA JSON repair processor core.

Phase M1 exposes parsing, safe schema validation, content-preservation checks,
and bounded prompt construction.  Phase M2 adds the validated settings model in
``json_processor.settings`` and persistence in ``json_processor.repository``.
Phase M3 adds the separate lease-based worker in ``json_processor.worker``.
Phase M4 adds the OpenAI-compatible client in ``json_processor.provider``.
Phase M5 adds orchestration in ``json_processor.service``; it is exported
lazily so importing the package root still does not import the database or
provider layers.  Phase M6 adds the admin API in ``json_processor.api``,
phase M7 adds the admin UI, and phase M8 adds opt-in Task/VFlow integration in
``json_processor.integration``.  Provider, worker, admin APIs, and Task/VFlow
integration remain opt-in submodules.
"""

from .content import (
    ContentAllowance,
    ContentChange,
    ContentChangeKind,
    ContentCheckResult,
    ContentOperation,
    check_content_preserved,
    diff_json_values,
    ensure_content_preserved,
    placeholder_allowance,
)
from .models import (
    ERROR_ENVELOPE_KEY,
    ERROR_ENVELOPE_STATUS,
    AttemptPhase,
    AttemptStatus,
    ParseResult,
    ParseStrategy,
    ProcessorError,
    ProcessorErrorCode,
    ProcessorFailurePolicy,
    RepairAttempt,
    RepairJobStatus,
    RepairMethod,
    RepairMode,
    RepairResult,
    RepairSourceType,
    SchemaDefinitionError,
    ValidationDiagnostic,
    ValidationResult,
    is_error_envelope,
)
from .parser import MAX_RAW_OUTPUT_BYTES, parse_json_output
from .prompt import (
    MAX_PROMPT_BYTES,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    PromptBudget,
    RepairPrompt,
    build_repair_prompt,
)
from .schema import (
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    is_valid,
    validate_schema,
    validate_value,
)
from .settings import (
    JsonProcessorSettings,
    SettingsValidationError,
    default_settings_json,
)

__all__ = [
    "ERROR_ENVELOPE_KEY",
    "ERROR_ENVELOPE_STATUS",
    "MAX_PROMPT_BYTES",
    "MAX_RAW_OUTPUT_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "UNTRUSTED_BEGIN",
    "UNTRUSTED_END",
    "AttemptPhase",
    "AttemptStatus",
    "ContentAllowance",
    "ContentChange",
    "ContentChangeKind",
    "ContentCheckResult",
    "ContentOperation",
    "JsonProcessorService",
    "JsonProcessorSettings",
    "JsonProcessorVFlowAdapter",
    "JsonRepairService",
    "ParseResult",
    "ParseStrategy",
    "ProcessorError",
    "ProcessorErrorCode",
    "ProcessorFailurePolicy",
    "PromptBudget",
    "RepairAttempt",
    "RepairJobStatus",
    "RepairMethod",
    "RepairMode",
    "RepairPrompt",
    "RepairResult",
    "RepairSourceType",
    "SchemaDefinitionError",
    "SettingsValidationError",
    "ValidationDiagnostic",
    "ValidationResult",
    "build_repair_prompt",
    "build_worker",
    "check_content_preserved",
    "default_settings_json",
    "diff_json_values",
    "ensure_content_preserved",
    "is_error_envelope",
    "is_valid",
    "parse_json_output",
    "placeholder_allowance",
    "resolve_env_api_key_ref",
    "validate_schema",
    "validate_value",
]


def __getattr__(name: str):
    """Lazily expose database/provider-aware orchestration.

    ``json_processor.service`` imports the repository and provider modules.
    Keeping it behind a module ``__getattr__`` preserves the core package's
    no-I/O import contract while still supporting
    ``from opencode_web.json_processor import JsonRepairService``.
    """

    if name in {"JsonRepairService", "JsonProcessorService"}:
        from .service import JsonProcessorService, JsonRepairService

        return {
            "JsonRepairService": JsonRepairService,
            "JsonProcessorService": JsonProcessorService,
        }[name]
    if name in {
        "JsonProcessorVFlowAdapter",
        "build_worker",
        "resolve_env_api_key_ref",
    }:
        from . import integration

        return getattr(integration, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
