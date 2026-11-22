"""Admin HTTP API for the standalone VALERIA JSON repair processor.

The API is deliberately independent of Task and VFlow: sandbox calls never
create repair jobs, never touch Task/VFlow rows, and never continue an OpenCode
session.  Reads require an administrator session; every mutation additionally
requires a CSRF token.  Errors use the same request-id envelope as VFlow.

Secrets never cross this boundary.  Settings contain only the opaque
``api_key_ref`` reference; resolved keys are used in-memory for a provider call
and are never serialized into a response, audit row, job, or metric.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import math
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..database import Database
from .models import (
    ProcessorError,
    ProcessorErrorCode,
    RepairAttempt,
    RepairJobStatus,
    RepairMethod,
    RepairResult,
    RepairSourceType,
)
from .provider import (
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ProviderError,
    validate_provider_base_url,
)
from .repository import (
    JsonProcessorRepository,
    JsonRepairJobNotFound,
    SettingsVersionConflict,
)
from .service import JsonRepairService
from .settings import (
    MAX_API_KEY_REF_CHARS,
    MAX_BASE_URL_CHARS,
    MAX_CONFIGURED_BYTES,
    MAX_MODEL_CHARS,
    JsonProcessorSettings,
    SettingsValidationError,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/admin/json-processor"

_SECRET_META_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "api_key_ref",
        "secret",
        "password",
        "access_token",
        "refresh_token",
    }
)
_JOB_SUMMARY_FIELDS = (
    "id",
    "source_type",
    "task_run_id",
    "vflow_run_id",
    "point_id",
    "raw_output_ref",
    "raw_hash",
    "schema_hash",
    "settings_version",
    "prompt_version",
    "status",
    "method",
    "attempts",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)
_ATTEMPT_FIELDS = (
    "id",
    "attempt_no",
    "method",
    "provider",
    "model",
    "validation_error",
    "status",
    "latency_ms",
    "token_usage",
    "created_at",
    "updated_at",
)
_FAILURE_CATEGORIES = {
    "NOT_REPAIRABLE": "parse",
    "AMBIGUOUS_JSON": "parse",
    "EMPTY_OUTPUT": "parse",
    "RAW_OUTPUT_TOO_LARGE": "parse",
    "SCHEMA_VALIDATION_FAILED": "schema",
    "SCHEMA_INVALID": "schema",
    "SCHEMA_DEFINITION_INVALID": "schema",
    "CONTENT_CHANGED": "content",
    "PROVIDER_ERROR": "provider",
    "LLM_UNAVAILABLE": "provider",
    "TIMEOUT": "provider",
    "NOT_CONFIGURED": "provider",
    "NOT_ENABLED": "provider",
    "CANCELLED": "cancelled",
    "INTERNAL_ERROR": "internal",
}
_FAILURE_CATEGORY_NAMES = (
    "parse",
    "schema",
    "content",
    "provider",
    "cancelled",
    "internal",
    "other",
)


class SettingsPatchBody(BaseModel):
    model_config = {"extra": "forbid"}

    expected_version: int | None = Field(default=None, ge=1)
    version: int | None = Field(default=None, ge=1)
    changes: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class ConnectionCheckBody(BaseModel):
    model_config = {"extra": "forbid"}

    base_url: str | None = Field(default=None, max_length=MAX_BASE_URL_CHARS)
    api_key_ref: str | None = Field(default=None, max_length=MAX_API_KEY_REF_CHARS)
    model: str | None = Field(default=None, max_length=MAX_MODEL_CHARS)


class SandboxBody(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}

    raw_output: str = Field(default="", max_length=MAX_CONFIGURED_BYTES)
    schema_definition: Any = Field(default=None, alias="schema")


class RetentionPurgeBody(BaseModel):
    model_config = {"extra": "forbid"}

    confirm: bool = False
    batch_size: int = Field(default=500, ge=1, le=5_000)


class JsonProcessorApiError(Exception):
    """A safe API failure rendered as the standard request-id envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id")
    if supplied and len(supplied) <= 200:
        return supplied
    return uuid.uuid4().hex


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": _request_id(request),
            }
        },
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _scrub_sensitive(value: Any) -> Any:
    """Drop secret-shaped keys from diagnostic metadata recursively."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _SECRET_META_KEYS or normalized.endswith(
                ("_secret", "_password", "_access_token", "_refresh_token")
            ):
                continue
            result[key] = _scrub_sensitive(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub_sensitive(item) for item in value]
    return value


def _audit_action(
    db: Database,
    actor: str | None,
    action: str,
    *,
    object_type: str = "json_processor",
    object_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Write one secret-free audit row; auditing must never break the response."""

    try:
        db.audit(
            actor,
            action,
            object_type=object_type,
            object_id=object_id,
            metadata=_scrub_sensitive(dict(metadata or {})),
        )
    except Exception:
        logger.warning("JSON processor audit action failed: %s", action)


def _percentile_latency(
    db: Database,
    percentile: float,
    samples: int,
) -> int | None:
    """Return the nearest-rank latency percentile from indexed attempt rows."""

    if samples <= 0:
        return None
    rank = max(1, math.ceil(percentile * samples))
    row = db._one(
        """SELECT latency_ms FROM json_repair_attempts
           WHERE latency_ms IS NOT NULL
           ORDER BY latency_ms, id LIMIT 1 OFFSET ?""",
        (min(samples - 1, rank - 1),),
    )
    if row is None:
        return None
    value = row["latency_ms"]
    return int(value) if isinstance(value, int) else None


def _failure_summary(error_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_category = {name: 0 for name in _FAILURE_CATEGORY_NAMES}
    by_code: dict[str, int] = {}
    for row in error_rows:
        code = str(row["error_code"])
        count = int(row["count"])
        by_code[code] = count
        category = _FAILURE_CATEGORIES.get(code, "other")
        by_category[category] += count
    return {
        "parse": by_category["parse"],
        "schema": by_category["schema"],
        "content": by_category["content"],
        "provider": by_category["provider"],
        "provider_errors": by_category["provider"],
        "cancelled": by_category["cancelled"],
        "internal": by_category["internal"],
        "other": by_category["other"],
        "by_category": by_category,
        "by_code": by_code,
    }


def _processor_error_body(error: ProcessorError) -> dict[str, Any]:
    return {
        "code": error.code.value,
        "message": error.message,
        "path": error.path,
        "diagnostics": [item.to_dict() for item in error.diagnostics],
    }


def _safe_bytes(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return 0


def _attempt_view(attempt: RepairAttempt) -> dict[str, Any]:
    """Serialize one sandbox attempt without its raw model output."""

    validation_error = None
    error = attempt.error
    if error is not None:
        validation_error = error.message
    return {
        "number": attempt.number,
        "phase": attempt.phase.value,
        "mode": attempt.mode.value,
        "ok": attempt.ok,
        "strategy": attempt.parse.strategy.value,
        "normalizations": list(attempt.parse.normalizations),
        "candidate_count": attempt.parse.candidate_count,
        "schema_valid": (
            None if attempt.validation is None else attempt.validation.valid
        ),
        "validation_error": validation_error,
        "error": None if error is None else _processor_error_body(error),
    }


def _sandbox_result_view(
    result: RepairResult,
    *,
    settings_version: int,
    raw_bytes: int,
) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "mode": result.mode.value,
        "method": result.method.value,
        "value": result.value if result.ok else None,
        "error": (
            None if result.error is None else _processor_error_body(result.error)
        ),
        "diagnostics": (
            []
            if result.error is None
            else [item.to_dict() for item in result.error.diagnostics]
        ),
        "attempts": [_attempt_view(item) for item in result.attempts],
        "settings_version": settings_version,
        "raw_input_bytes": raw_bytes,
        "job_created": False,
    }


def _settings_view(settings: Mapping[str, Any]) -> dict[str, Any]:
    view = dict(settings)
    view["provider_configured"] = bool(view.get("base_url") and view.get("model"))
    view["api_key_ref_configured"] = bool(view.get("api_key_ref"))
    return view


def _job_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in _JOB_SUMMARY_FIELDS}


def _job_detail(
    job: Mapping[str, Any],
    attempts: list[Mapping[str, Any]],
) -> dict[str, Any]:
    view = _job_summary(job)
    attempt_views = [_stored_attempt_view(item) for item in attempts]
    view["attempts_count"] = view.pop("attempts", 0)
    view["attempts"] = attempt_views
    # Explicit alias retained for clients that prefer the descriptive name.
    view["attempts_history"] = attempt_views
    view.update(
        {
            "schema": job.get("schema"),
            "result": job.get("result"),
            "has_result": bool(job.get("has_result")),
            "idempotency_key": job.get("idempotency_key"),
            "lease_owner": job.get("lease_owner"),
            "lease_until": job.get("lease_until"),
        }
    )
    return view


def _stored_attempt_view(attempt: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        key: attempt.get(key)
        for key in _ATTEMPT_FIELDS
        if key in attempt
    }
    view["request_meta"] = _scrub_sensitive(attempt.get("request_meta") or {})
    return view


def _job_status(value: str | None) -> RepairJobStatus | None:
    if value is None or value == "":
        return None
    try:
        return RepairJobStatus(value)
    except ValueError:
        raise JsonProcessorApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ProcessorErrorCode.INVALID_INPUT.value,
            "Unknown repair job status",
            {"allowed": [item.value for item in RepairJobStatus]},
        ) from None


def _job_source(value: str | None) -> RepairSourceType | None:
    if value is None or value == "":
        return None
    try:
        return RepairSourceType(value)
    except ValueError:
        raise JsonProcessorApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ProcessorErrorCode.INVALID_INPUT.value,
            "Unknown repair job source type",
            {"allowed": [item.value for item in RepairSourceType]},
        ) from None


def _job_method(value: str | None) -> RepairMethod | None:
    if value is None or value == "":
        return None
    try:
        return RepairMethod(value)
    except ValueError:
        raise JsonProcessorApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ProcessorErrorCode.INVALID_INPUT.value,
            "Unknown repair method",
            {"allowed": [item.value for item in RepairMethod]},
        ) from None


def register_json_processor(
    app: FastAPI,
    database: Callable[[Request], Database],
    admin_principal: Callable[..., dict[str, Any]],
    admin_read_principal: Callable[..., dict[str, Any]],
    *,
    allow_private_networks: bool = False,
) -> None:
    """Register the JSON processor admin API on ``app``."""

    repository = JsonProcessorRepository
    allow_private = bool(allow_private_networks)
    app.state.json_processor_allow_private_networks = allow_private

    def repo(db: Database) -> JsonProcessorRepository:
        return repository(db)

    def provider_factory(request: Request) -> Callable[[Any], Any] | None:
        factory = getattr(
            request.app.state, "json_processor_provider_factory", None
        )
        return factory if callable(factory) else None

    def api_key_resolver(request: Request) -> Callable[..., Any] | None:
        resolver = getattr(
            request.app.state, "json_processor_api_key_resolver", None
        )
        return resolver if callable(resolver) else None

    async def resolve_provider(
        request: Request,
        settings: JsonProcessorSettings,
    ) -> tuple[Any, bool]:
        factory = provider_factory(request)
        if factory is not None:
            try:
                provided = await _maybe_await(factory(settings))
            except Exception:
                raise JsonProcessorApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    ProcessorErrorCode.NOT_CONFIGURED.value,
                    "provider configuration is invalid",
                ) from None
            if provided is None:
                raise JsonProcessorApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    ProcessorErrorCode.NOT_CONFIGURED.value,
                    "JSON repair provider is not configured",
                )
            return provided, False
        try:
            return (
                OpenAICompatibleProvider(
                    settings,
                    allow_private_networks=allow_private,
                ),
                True,
            )
        except ProviderError as exc:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                exc.message,
            ) from None

    async def resolve_api_key(
        request: Request,
        settings: JsonProcessorSettings,
    ) -> str | None:
        resolver = api_key_resolver(request)
        if resolver is None or not settings.api_key_ref:
            return None
        try:
            value = await _maybe_await(resolver(settings.api_key_ref))
        except Exception:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                "Provider credentials are unavailable",
            ) from None
        if value is not None and not isinstance(value, str):
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                "Provider credentials are unavailable",
            )
        return value

    def enabled_settings(
        db: Database,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> tuple[JsonProcessorSettings, int]:
        stored = repo(db).get_settings()
        try:
            data = JsonProcessorSettings.from_dict(
                stored, reject_unknown=False
            ).to_dict()
            if overrides:
                data.update(
                    {
                        key: value
                        for key, value in overrides.items()
                        if value is not None
                    }
                )
            settings = JsonProcessorSettings.from_dict(data)
        except SettingsValidationError as exc:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                str(exc),
                {"errors": list(exc.errors)},
            ) from None
        return settings, int(stored["version"])

    @app.exception_handler(JsonProcessorApiError)
    async def json_processor_api_error(
        request: Request, exc: JsonProcessorApiError
    ) -> JSONResponse:
        return _error_response(
            request, exc.status_code, exc.code, exc.message, exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def json_processor_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not request.url.path.startswith(API_PREFIX):
            # Preserve FastAPI's normal validation shape for every other module.
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"detail": jsonable_encoder(exc.errors())},
            )
        details = [
            {
                "loc": list(item.get("loc") or ()),
                "msg": item.get("msg"),
                "type": item.get("type"),
            }
            for item in exc.errors()
        ]
        return _error_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ProcessorErrorCode.INVALID_INPUT.value,
            "Request validation failed",
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def json_processor_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if not request.url.path.startswith(API_PREFIX):
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        codes = {
            status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
            status.HTTP_403_FORBIDDEN: "FORBIDDEN",
            status.HTTP_404_NOT_FOUND: "NOT_FOUND",
            status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        }
        detail = exc.detail
        message = detail if isinstance(detail, str) else "request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": codes.get(exc.status_code, f"HTTP_{exc.status_code}"),
                    "message": message,
                    "details": None if isinstance(detail, str) else detail,
                    "request_id": _request_id(request),
                }
            },
            headers=exc.headers,
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @app.get(f"{API_PREFIX}/settings")
    async def get_json_processor_settings(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return _settings_view(repo(db).get_settings())

    @app.patch(f"{API_PREFIX}/settings")
    async def patch_json_processor_settings(
        body: SettingsPatchBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> Any:
        actor = principal["user"]["id"]
        expected_version = body.expected_version or body.version
        if expected_version is None:
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                "expected_version is required",
            )
        if body.changes is not None and body.settings is not None:
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                "pass either changes or settings, not both",
            )
        changes = body.changes if body.changes is not None else body.settings
        if changes is None:
            changes = {}
        changed_fields = sorted(changes)
        try:
            current = repo(db).get_settings()
            candidate = {**current, **changes}
            candidate_base_url = candidate.get("base_url")
            if isinstance(candidate_base_url, str) and candidate_base_url:
                validate_provider_base_url(
                    candidate_base_url,
                    allow_private_networks=allow_private,
                )
        except ProviderConfigurationError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.settings.rejected",
                object_type="json_processor_settings",
                object_id="1",
                metadata={
                    "changed_fields": changed_fields,
                    "reason": exc.message,
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                exc.message,
                {"errors": [exc.message]},
            )
        try:
            updated = repo(db).update_settings(
                changes,
                expected_version=expected_version,
                updated_by=actor,
            )
        except SettingsVersionConflict as exc:
            _audit_action(
                db,
                actor,
                "json_processor.settings.update_conflict",
                object_type="json_processor_settings",
                object_id="1",
                metadata={
                    "expected_version": exc.expected,
                    "current_version": exc.actual,
                },
            )
            return _error_response(
                request,
                status.HTTP_409_CONFLICT,
                ProcessorErrorCode.SETTINGS_VERSION_CONFLICT.value,
                str(exc),
                {
                    "expected_version": exc.expected,
                    "current_version": exc.actual,
                },
            )
        except SettingsValidationError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.settings.rejected",
                object_type="json_processor_settings",
                object_id="1",
                metadata={
                    "changed_fields": changed_fields,
                    "errors": list(exc.errors),
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                str(exc),
                {"errors": list(exc.errors)},
            )
        except ValueError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.settings.rejected",
                object_type="json_processor_settings",
                object_id="1",
                metadata={
                    "changed_fields": changed_fields,
                    "reason": str(exc),
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                str(exc),
            )
        _audit_action(
            db,
            actor,
            "json_processor.settings.update",
            object_type="json_processor_settings",
            object_id="1",
            metadata={
                "expected_version": expected_version,
                "version": updated.get("version"),
                "changed_fields": changed_fields,
            },
        )
        return _settings_view(updated)

    @app.get(f"{API_PREFIX}/providers")
    async def get_json_processor_providers(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        settings = repo(db).get_settings()
        return {
            "protocol": "openai-compatible",
            "description": (
                "Only non-streaming OpenAI-compatible Chat Completions is "
                "supported. No tools or function calling are used."
            ),
            "endpoints": {
                "chat_completions": "/v1/chat/completions",
                "models": "/v1/models",
            },
            "allow_private_networks": allow_private,
            "auth": {
                "type": "bearer",
                "secret_reference_field": "api_key_ref",
                "secret_value_exposed": False,
                "optional": True,
            },
            "fields": [
                {
                    "name": "base_url",
                    "type": "string",
                    "required_for_llm": True,
                    "sensitive": False,
                },
                {
                    "name": "api_key_ref",
                    "type": "string",
                    "required_for_llm": False,
                    "sensitive": True,
                    "description": "Opaque reference only; never the key itself.",
                },
                {
                    "name": "model",
                    "type": "string",
                    "required_for_llm": True,
                    "sensitive": False,
                },
            ],
            "presets": [
                {
                    "id": "openai",
                    "label": "OpenAI",
                    "base_url": "https://api.openai.com",
                    "model_hint": "gpt-4o-mini",
                    "requires_api_key_ref": True,
                },
                {
                    "id": "local",
                    "label": "Локальный OpenAI-compatible",
                    "base_url": "http://127.0.0.1:1234",
                    "model_hint": "local-model",
                    "requires_api_key_ref": False,
                },
                {
                    "id": "custom",
                    "label": "Другой OpenAI-compatible",
                    "base_url": "",
                    "model_hint": "",
                    "requires_api_key_ref": False,
                },
            ],
            "current": {
                "enabled": bool(settings.get("enabled")),
                "base_url": settings.get("base_url") or "",
                "model": settings.get("model") or "",
                "api_key_ref_configured": bool(settings.get("api_key_ref")),
                "provider_configured": bool(
                    settings.get("base_url") and settings.get("model")
                ),
                "settings_version": settings.get("version"),
            },
        }

    @app.post(f"{API_PREFIX}/check-connection")
    async def check_json_processor_connection(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
        body: ConnectionCheckBody | None = None,
    ) -> Any:
        body = body or ConnectionCheckBody()
        actor = principal["user"]["id"]
        try:
            settings, settings_version = enabled_settings(
                db,
                overrides={
                    "base_url": body.base_url,
                    "api_key_ref": body.api_key_ref,
                    "model": body.model,
                },
            )
        except JsonProcessorApiError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "error",
                    "error_code": exc.code,
                    "provider_configured": False,
                },
            )
            raise
        if not settings.base_url or not settings.model:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "not_configured",
                    "settings_version": settings_version,
                    "base_url_configured": bool(settings.base_url),
                    "model_configured": bool(settings.model),
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                "base_url and model are required for a connection check",
                {
                    "base_url_configured": bool(settings.base_url),
                    "model_configured": bool(settings.model),
                },
            )
        try:
            validate_provider_base_url(
                settings.base_url,
                allow_private_networks=allow_private,
            )
        except ProviderConfigurationError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "blocked_base_url",
                    "settings_version": settings_version,
                    "model": settings.model,
                    "reason": exc.message,
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.SETTINGS_INVALID.value,
                exc.message,
                {"errors": [exc.message]},
            )
        try:
            provider, owned = await resolve_provider(request, settings)
        except JsonProcessorApiError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "error",
                    "settings_version": settings_version,
                    "model": settings.model,
                    "error_code": exc.code,
                },
            )
            raise
        try:
            api_key = await resolve_api_key(request, settings)
            connection = await _maybe_await(
                provider.check_connection(
                    api_key=api_key,
                    timeout=float(settings.request_timeout_seconds),
                )
            )
        except ProviderError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "provider_error",
                    "settings_version": settings_version,
                    "model": settings.model,
                    "status_code": exc.status_code,
                    "provider_code": exc.provider_code,
                },
            )
            return {
                "ok": False,
                "settings_version": settings_version,
                "base_url": settings.base_url,
                "model": settings.model,
                "error": _scrub_sensitive(exc.to_dict()),
            }
        except JsonProcessorApiError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "error",
                    "settings_version": settings_version,
                    "model": settings.model,
                    "error_code": exc.code,
                },
            )
            raise
        except Exception:
            _audit_action(
                db,
                actor,
                "json_processor.connection_check",
                metadata={
                    "outcome": "error",
                    "settings_version": settings_version,
                    "model": settings.model,
                },
            )
            return {
                "ok": False,
                "settings_version": settings_version,
                "base_url": settings.base_url,
                "model": settings.model,
                "error": {
                    "code": "connection_check_failed",
                    "processor_code": ProcessorErrorCode.PROVIDER_ERROR.value,
                    "message": "provider connection check failed",
                },
            }
        finally:
            if owned:
                close = getattr(provider, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await _maybe_await(close())
        _audit_action(
            db,
            actor,
            "json_processor.connection_check",
            metadata={
                "outcome": "ok" if connection.ok else "failed",
                "settings_version": settings_version,
                "model": settings.model,
                "status_code": connection.status_code,
                "model_available": connection.model_available,
                "latency_ms": connection.latency_ms,
            },
        )
        return {
            "ok": bool(connection.ok),
            "settings_version": settings_version,
            "base_url": settings.base_url,
            "model": settings.model,
            "status_code": connection.status_code,
            "model_available": connection.model_available,
            "latency_ms": connection.latency_ms,
        }

    @app.post(f"{API_PREFIX}/test")
    async def test_json_processor(
        body: SandboxBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> Any:
        actor = principal["user"]["id"]
        raw_bytes = _safe_bytes(body.raw_output)
        try:
            settings, settings_version = enabled_settings(db)
        except JsonProcessorApiError as exc:
            _audit_action(
                db,
                actor,
                "json_processor.sandbox.test",
                metadata={
                    "outcome": "error",
                    "error_code": exc.code,
                    "raw_input_bytes": raw_bytes,
                },
            )
            raise

        def audit_test(outcome: str, **extra: Any) -> None:
            _audit_action(
                db,
                actor,
                "json_processor.sandbox.test",
                metadata={
                    "outcome": outcome,
                    "settings_version": settings_version,
                    "raw_input_bytes": raw_bytes,
                    "schema_present": body.schema_definition is not None,
                    **extra,
                },
            )

        if not settings.enabled:
            audit_test("not_enabled", error_code=ProcessorErrorCode.NOT_ENABLED.value)
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_ENABLED.value,
                "JSON repair processor is disabled",
                {"enabled": False},
            )
        if settings.max_llm_attempts > 0 and (
            not settings.base_url or not settings.model
        ):
            audit_test(
                "not_configured",
                error_code=ProcessorErrorCode.NOT_CONFIGURED.value,
                base_url_configured=bool(settings.base_url),
                model_configured=bool(settings.model),
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                "LLM repair is enabled but base_url or model is not configured",
                {
                    "base_url_configured": bool(settings.base_url),
                    "model_configured": bool(settings.model),
                },
            )
        if settings.max_llm_attempts == 0 and not settings.local_repair_enabled:
            audit_test(
                "not_configured",
                error_code=ProcessorErrorCode.NOT_CONFIGURED.value,
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.NOT_CONFIGURED.value,
                "No repair path is configured",
            )
        service = JsonRepairService(
            repo(db),
            settings=settings,
            provider_factory=provider_factory(request),
            api_key_resolver=api_key_resolver(request),
            allow_private_networks=allow_private,
        )
        try:
            result = await service.process_task_output(
                body.raw_output,
                body.schema_definition,
            )
        except asyncio.CancelledError:
            audit_test("cancelled", error_code=ProcessorErrorCode.CANCELLED.value)
            raise
        except Exception:
            audit_test("error", error_code=ProcessorErrorCode.INTERNAL_ERROR.value)
            return _error_response(
                request,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                ProcessorErrorCode.INTERNAL_ERROR.value,
                "internal processor error",
            )
        audit_test(
            "ok" if result.ok else "rejected",
            ok=bool(result.ok),
            method=result.method.value,
            mode=result.mode.value,
            attempts=len(result.attempts),
            error_code=None if result.error is None else result.error.code.value,
        )
        return _sandbox_result_view(
            result,
            settings_version=settings_version,
            raw_bytes=raw_bytes,
        )

    # ------------------------------------------------------------------
    # Jobs and metrics
    # ------------------------------------------------------------------
    @app.get(f"{API_PREFIX}/jobs")
    async def list_json_processor_jobs(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        job_status: Annotated[str | None, Query(alias="status")] = None,
        source_type: str | None = None,
        method: str | None = None,
        error_code: str | None = None,
        task_run_id: str | None = None,
        vflow_run_id: str | None = None,
        point_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.INVALID_INPUT.value,
                "limit must be between 1 and 200",
            )
        if offset < 0:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.INVALID_INPUT.value,
                "offset must be non-negative",
            )
        try:
            page = repo(db).list_jobs(
                status=_job_status(job_status),
                source_type=_job_source(source_type),
                method=_job_method(method),
                task_run_id=task_run_id,
                vflow_run_id=vflow_run_id,
                point_id=point_id,
                error_code=error_code,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise JsonProcessorApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.INVALID_INPUT.value,
                str(exc),
            ) from None
        return {
            "items": [_job_summary(item) for item in page["items"]],
            "total": page["total"],
            "limit": page["limit"],
            "offset": page["offset"],
            "has_more": page["offset"] + len(page["items"]) < page["total"],
        }

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}")
    async def get_json_processor_job(
        job_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> Any:
        try:
            job = repo(db).get_job(job_id)
            if job is None:
                raise JsonRepairJobNotFound("repair job not found")
            attempts = repo(db).list_attempts(job_id)
        except JsonRepairJobNotFound:
            return _error_response(
                request,
                status.HTTP_404_NOT_FOUND,
                ProcessorErrorCode.JOB_NOT_FOUND.value,
                "Repair job not found",
            )
        return _job_detail(job, attempts)

    @app.get(f"{API_PREFIX}/metrics")
    async def get_json_processor_metrics(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        processor = repo(db)
        settings = processor.get_settings()
        queue = processor.queue_depth()

        status_rows = db._all(
            """SELECT status, COUNT(*) AS count FROM json_repair_jobs
               GROUP BY status"""
        )
        method_rows = db._all(
            """SELECT method, COUNT(*) AS count FROM json_repair_jobs
               GROUP BY method"""
        )
        source_rows = db._all(
            """SELECT source_type, COUNT(*) AS count FROM json_repair_jobs
               GROUP BY source_type"""
        )
        error_rows = db._all(
            """SELECT error_code, COUNT(*) AS count FROM json_repair_jobs
               WHERE error_code IS NOT NULL
               GROUP BY error_code ORDER BY count DESC, error_code"""
        )
        job_counts = {
            str(row["status"]): int(row["count"]) for row in status_rows
        }
        method_counts = {
            str(row["method"]): int(row["count"]) for row in method_rows
        }
        source_counts = {
            str(row["source_type"]): int(row["count"])
            for row in source_rows
        }
        total = sum(job_counts.values())
        terminal = sum(
            job_counts.get(item.value, 0) for item in RepairJobStatus.terminal()
        )
        succeeded = job_counts.get(RepairJobStatus.SUCCEEDED.value, 0)
        failed = job_counts.get(RepairJobStatus.FAILED.value, 0)
        failures = _failure_summary(error_rows)

        attempt_stats = db._one(
            """SELECT COUNT(*) AS total,
                      COUNT(latency_ms) AS latency_samples,
                      AVG(latency_ms) AS avg_latency_ms,
                      MAX(latency_ms) AS max_latency_ms
               FROM json_repair_attempts"""
        ) or {}
        latency_samples = int(attempt_stats.get("latency_samples") or 0)
        p50_latency = _percentile_latency(db, 0.50, latency_samples)
        p95_latency = _percentile_latency(db, 0.95, latency_samples)
        max_latency = attempt_stats.get("max_latency_ms")
        attempt_method_rows = db._all(
            """SELECT method, COUNT(*) AS count FROM json_repair_attempts
               GROUP BY method"""
        )
        attempt_status_rows = db._all(
            """SELECT status, COUNT(*) AS count FROM json_repair_attempts
               GROUP BY status"""
        )
        token_rows = db._all(
            "SELECT token_usage_json FROM json_repair_attempts "
            "WHERE token_usage_json IS NOT NULL"
        )
        token_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        for row in token_rows:
            try:
                usage = json.loads(row["token_usage_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(usage, Mapping):
                continue
            for key in token_totals:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    token_totals[key] += value

        now = datetime.now(UTC)
        day_ago = (now - timedelta(hours=24)).isoformat()
        recent = db._one(
            """SELECT COUNT(*) AS created FROM json_repair_jobs
               WHERE created_at >= ?""",
            (day_ago,),
        ) or {}
        recent_finished = db._one(
            """SELECT
                   SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END)
                       AS succeeded,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                       AS failed,
                   SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END)
                       AS cancelled
               FROM json_repair_jobs
               WHERE finished_at IS NOT NULL AND finished_at >= ?""",
            (day_ago,),
        ) or {}
        recent_attempts = db._one(
            """SELECT COUNT(*) AS count FROM json_repair_attempts
               WHERE created_at >= ?""",
            (day_ago,),
        ) or {}
        provider_codes = tuple(
            code
            for code, category in _FAILURE_CATEGORIES.items()
            if category == "provider"
        )
        provider_placeholders = ", ".join("?" for _ in provider_codes)
        recent_provider_errors = db._one(
            f"""SELECT COUNT(*) AS count FROM json_repair_jobs
                WHERE finished_at IS NOT NULL AND finished_at >= ?
                  AND error_code IN ({provider_placeholders})""",
            (day_ago, *provider_codes),
        ) or {}
        average_latency = attempt_stats.get("avg_latency_ms")
        retention = processor.retention_preview(now)
        return {
            "settings": {
                "enabled": bool(settings.get("enabled")),
                "version": settings.get("version"),
                "base_url": settings.get("base_url") or "",
                "model": settings.get("model") or "",
                "provider_configured": bool(
                    settings.get("base_url") and settings.get("model")
                ),
                "api_key_ref_configured": bool(settings.get("api_key_ref")),
                "updated_at": settings.get("updated_at"),
            },
            "queue": queue,
            "jobs": {
                "total": total,
                "terminal": terminal,
                "by_status": job_counts,
                "by_method": method_counts,
                "by_source": source_counts,
                "by_error_code": {
                    str(row["error_code"]): int(row["count"])
                    for row in error_rows
                },
                "success_rate": (
                    round(succeeded / terminal, 4) if terminal else None
                ),
                "succeeded": succeeded,
                "failed": failed,
            },
            "failures": failures,
            "attempts": {
                "total": int(attempt_stats.get("total") or 0),
                "avg_latency_ms": (
                    None
                    if average_latency is None
                    else round(float(average_latency), 1)
                ),
                "latency": {
                    "samples": latency_samples,
                    "p50_ms": p50_latency,
                    "p95_ms": p95_latency,
                    "max_ms": (
                        int(max_latency)
                        if isinstance(max_latency, int)
                        else None
                    ),
                },
                "p50_latency_ms": p50_latency,
                "p95_latency_ms": p95_latency,
                "by_method": {
                    str(row["method"]): int(row["count"])
                    for row in attempt_method_rows
                },
                "by_status": {
                    str(row["status"]): int(row["count"])
                    for row in attempt_status_rows
                },
                "tokens": token_totals,
            },
            "last_24h": {
                "jobs_created": int(recent.get("created") or 0),
                "succeeded": int(recent_finished.get("succeeded") or 0),
                "failed": int(recent_finished.get("failed") or 0),
                "cancelled": int(recent_finished.get("cancelled") or 0),
                "attempts": int(recent_attempts.get("count") or 0),
                "provider_errors": int(recent_provider_errors.get("count") or 0),
            },
            "retention": retention,
        }

    # ------------------------------------------------------------------
    # Retention preview and explicit purge
    # ------------------------------------------------------------------
    @app.get(f"{API_PREFIX}/retention")
    async def get_json_processor_retention(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return {
            "dry_run": True,
            **repo(db).retention_preview(),
        }

    @app.post(f"{API_PREFIX}/retention/purge")
    async def purge_json_processor_retention(
        body: RetentionPurgeBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> Any:
        processor = repo(db)
        preview = processor.retention_preview()
        actor = principal["user"]["id"]
        if not body.confirm:
            _audit_action(
                db,
                actor,
                "json_processor.retention.purge",
                metadata={
                    "outcome": "confirmation_required",
                    "jobs_with_expired_raw_output": preview[
                        "jobs_with_expired_raw_output"
                    ],
                    "expired_attempts": preview["expired_attempts"],
                },
            )
            return _error_response(
                request,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ProcessorErrorCode.INVALID_INPUT.value,
                "retention purge requires confirm=true",
                {
                    "dry_run": True,
                    "preview": preview,
                },
            )
        result = processor.retention_purge(batch_size=body.batch_size)
        _audit_action(
            db,
            actor,
            "json_processor.retention.purge",
            metadata={
                "outcome": "purged",
                "batch_size": result["batch_size"],
                "jobs_output_purged": result["jobs_output_purged"],
                "attempts_deleted": result["attempts_deleted"],
                "has_more": result["has_more"],
                "raw_output_cutoff": result["raw_output_cutoff"],
                "attempt_cutoff": result["attempt_cutoff"],
            },
        )
        return {
            "ok": True,
            "dry_run": False,
            **result,
        }


__all__ = [
    "API_PREFIX",
    "ConnectionCheckBody",
    "JsonProcessorApiError",
    "RetentionPurgeBody",
    "SandboxBody",
    "SettingsPatchBody",
    "register_json_processor",
]
