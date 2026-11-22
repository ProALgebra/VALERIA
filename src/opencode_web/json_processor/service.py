"""Service orchestration for the standalone JSON repair processor.

The service is the only M5 component that combines the pure core modules with
persistence and the OpenAI-compatible provider.  It has two entry points:

``process_task_output``
    Lower-level, job-less processing of one raw model output.  It returns a
    :class:`~opencode_web.json_processor.models.RepairResult` and never mutates
    the supplied string.

``process_job``
    Worker processor callback ``(job, lease_token)``.  It loads the raw output
    through an injected loader, runs the same pipeline, records one row per
    local/LLM attempt, and finalizes the job through the repository's
    lease-aware compare-and-set operations.  Repeated calls for a terminal job
    return the stored result without invoking the provider or adding attempts.

Safety rules
------------
* Raw output is treated as immutable untrusted data.  Its SHA-256 must match
  the job reference before processing starts.
* A strict parse is attempted first.  Local normalization is only a fallback.
* Every accepted value must satisfy the configured schema subset and the
  content-preservation check.  ``syntax_only`` never permits a value, key, or
  item change; ``schema_safe`` permits only explicit per-request
  :class:`ContentAllowance` paths.
* Provider calls are bounded by ``max_llm_attempts``.  The provider's internal
  ``response_format`` fallback is infrastructure-only and is not counted.
* No database transaction spans a provider call.  Every repository operation
  is a short synchronous call moved off the event loop with ``asyncio.to_thread``.
* Error messages, attempt metadata, and results never contain raw output,
  provider bodies, API keys, or other secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .content import ContentAllowance, check_content_preserved
from .models import (
    AttemptPhase,
    AttemptStatus,
    ParseResult,
    ProcessorError,
    ProcessorErrorCode,
    RepairAttempt,
    RepairJobStatus,
    RepairMethod,
    RepairMode,
    RepairResult,
    SchemaDefinitionError,
    ValidationDiagnostic,
)
from .parser import parse_json_output
from .prompt import build_repair_prompt
from .provider import (
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorKind,
)
from .repository import (
    JsonProcessorRepository,
    JsonRepairJobNotFound,
    JsonRepairLeaseLost,
    JsonRepairStateConflict,
)
from .schema import validate_schema, validate_value
from .settings import (
    JsonProcessorSettings,
    SettingsValidationError,
)
from .worker import ProcessorOutcome

logger = logging.getLogger(__name__)

SettingsValue = JsonProcessorSettings | Mapping[str, Any]
SettingsLoader = Callable[[], SettingsValue | Awaitable[SettingsValue]]
ProviderFactory = Callable[[JsonProcessorSettings], Any]
RawOutputLoader = Callable[[str], str | Awaitable[str]]
RawOutputStore = Callable[[str, str], Any]
CancellationCheck = Callable[[Mapping[str, Any]], bool | Awaitable[bool]]
AllowanceProvider = Callable[[Mapping[str, Any]], Iterable[ContentAllowance]]
ApiKeyResolver = Callable[[str], str | None | Awaitable[str | None]]

_MISSING = object()
_MAX_MESSAGE_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class _AttemptAudit:
    """Everything needed to persist one attempt without raw output."""

    attempt: RepairAttempt
    method: RepairMethod
    status: AttemptStatus
    provider: str | None = None
    model: str | None = None
    request_meta: Mapping[str, Any] = field(default_factory=dict)
    validation_error: str | None = None
    latency_ms: int | None = None
    token_usage: Mapping[str, int] | None = None


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON constant {value!r}")


def _strict_load(raw_output: str) -> tuple[Any, bool]:
    try:
        return json.loads(raw_output, parse_constant=_reject_constant), True
    except (TypeError, ValueError):
        return None, False


def _canonical_json(value: Any) -> str | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None


def _safe_text(value: Any, *, fallback: str = "", maximum: int = _MAX_MESSAGE_CHARS) -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if not text:
        return fallback
    return text[:maximum]


def _redact_secret(value: Any, secret: str | None) -> str:
    """Remove an in-memory credential from a provider message or error."""

    text = _safe_text(value)
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def _diagnostics_for(error: ProcessorError) -> tuple[ValidationDiagnostic, ...]:
    if error.diagnostics:
        return error.diagnostics
    return (
        ValidationDiagnostic(
            code=error.code.value,
            message=_safe_text(error.message, fallback="validation failed"),
            path=error.path or "$",
        ),
    )


def _validation_text(error: ProcessorError) -> str:
    parts = [
        f"{item.path}: {_safe_text(item.message, fallback=item.code, maximum=500)}"
        for item in error.diagnostics[:20]
    ]
    if parts:
        return _safe_text(
            f"{error.code.value}: {'; '.join(parts)}",
            fallback=error.code.value,
        )
    return _safe_text(
        f"{error.code.value}: {error.message}",
        fallback=error.code.value,
    )


def _error_message(error: ProcessorError) -> str:
    message = _safe_text(error.message, fallback=error.code.value)
    if error.diagnostics:
        first = error.diagnostics[0]
        detail = _safe_text(first.message, fallback=first.code, maximum=500)
        if detail and detail not in message:
            message = f"{message}: {first.path}: {detail}"
    return _safe_text(message, fallback=error.code.value)


def _mapping_value(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else default


def _lease_is_active(job: Mapping[str, Any], lease_token: str) -> bool:
    if job.get("status") != RepairJobStatus.PROCESSING.value:
        return False
    if job.get("lease_owner") != lease_token:
        return False
    lease_until = job.get("lease_until")
    if not isinstance(lease_until, str):
        return False
    try:
        moment = datetime.fromisoformat(lease_until)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment > datetime.now(UTC)


def _completion_field(completion: Any, name: str, default: Any = None) -> Any:
    """Read a field from a provider result object or mapping."""

    if isinstance(completion, Mapping):
        return completion.get(name, default)
    return getattr(completion, name, default)


def _coerce_method(value: RepairMethod | str | None) -> RepairMethod:
    if value is None:
        return RepairMethod.NONE
    if isinstance(value, RepairMethod):
        return value
    return RepairMethod(value)


def _method_for_attempts(attempts: Iterable[RepairAttempt]) -> RepairMethod:
    items = tuple(attempts)
    if any(item.phase is AttemptPhase.LLM for item in items):
        return RepairMethod.LLM
    if any(item.phase is AttemptPhase.LOCAL for item in items):
        return RepairMethod.LOCAL
    return RepairMethod.NONE


async def _invoke(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a sync or async injected dependency without blocking the loop."""

    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    result = await asyncio.to_thread(func, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class JsonRepairService:
    """Process one JSON repair job or one raw task output.

    ``repository`` is optional for pure ``process_task_output`` use.  Job
    processing requires it.  Settings are resolved in this order: explicit
    per-call value, static constructor value, injected loader, repository
    singleton settings, then validated defaults.
    """

    def __init__(
        self,
        repository: JsonProcessorRepository | None = None,
        settings: SettingsValue | None = None,
        *,
        settings_loader: SettingsLoader | None = None,
        settings_provider: SettingsLoader | None = None,
        provider: Any | None = None,
        provider_factory: ProviderFactory | None = None,
        provider_name: str | None = None,
        api_key: str | None = None,
        api_key_resolver: ApiKeyResolver | None = None,
        raw_output_loader: RawOutputLoader | None = None,
        raw_loader: RawOutputLoader | None = None,
        raw_output_store: RawOutputStore | None = None,
        cancellation_check: CancellationCheck | None = None,
        is_cancelled: CancellationCheck | None = None,
        allowances: Iterable[ContentAllowance] = (),
        allowance_provider: AllowanceProvider | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        if provider is not None and provider_factory is not None:
            raise ValueError("pass either provider or provider_factory, not both")
        loader = settings_loader if settings_loader is not None else settings_provider
        raw_loader_callable = (
            raw_output_loader if raw_output_loader is not None else raw_loader
        )
        cancellation = (
            cancellation_check if cancellation_check is not None else is_cancelled
        )
        self._repository = repository
        self._settings = self._coerce_settings(settings) if settings is not None else None
        self._settings_loader = loader
        self._provider = provider
        self._provider_factory = provider_factory
        self._provider_name = provider_name
        self._api_key = api_key
        self._api_key_resolver = api_key_resolver
        self._raw_loader = raw_loader_callable
        self._raw_store = raw_output_store
        self._cancellation_check = cancellation
        self._allowances = tuple(allowances)
        self._allowance_provider = allowance_provider
        self._allow_private_networks = bool(allow_private_networks)
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._job_locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    async def __call__(self, job: dict[str, Any], lease_token: str) -> ProcessorOutcome:
        return await self.process_job(job, lease_token)

    async def process_claimed_job(
        self,
        job: dict[str, Any],
        lease_token: str,
        schema: Any = None,
        **kwargs: Any,
    ) -> ProcessorOutcome:
        """Alias for :meth:`process_job` for explicit worker wiring."""

        return await self.process_job(job, lease_token, schema, **kwargs)

    async def process_task_output(
        self,
        raw_output: str,
        schema: Any = None,
        *,
        repair_mode: RepairMode | str | None = None,
        mode: RepairMode | str | None = None,
        allowances: Iterable[ContentAllowance] | None = None,
        content_allowances: Iterable[ContentAllowance] | None = None,
        settings: SettingsValue | None = None,
        provider: Any | None = None,
        api_key: str | None = None,
        max_llm_attempts: int | None = None,
        raw_output_ref: str | None = None,
        persist_raw_output: bool = False,
    ) -> RepairResult:
        """Process raw model output without a persisted job.

        The input string is never modified.  A failure is returned as a
        fail-closed :class:`RepairResult` whose ``error`` is a typed
        :class:`ProcessorError`; callers must not use ``value`` when
        ``ok`` is false.
        """

        resolved_mode = RepairMode.SYNTAX_ONLY
        try:
            if content_allowances is not None:
                if allowances is not None:
                    raise ProcessorError(
                        ProcessorErrorCode.INVALID_INPUT,
                        "pass either allowances or content_allowances, not both",
                    )
                allowances = content_allowances
            if mode is not None:
                if repair_mode is not None:
                    raise ProcessorError(
                        ProcessorErrorCode.INVALID_INPUT,
                        "pass either repair_mode or mode, not both",
                    )
                repair_mode = mode
            resolved_settings = await self._resolve_settings(settings)
            resolved_mode = self._resolve_mode(repair_mode, resolved_settings)
            resolved_allowances = self._resolve_allowances(allowances)
            attempts_allowed = self._resolve_max_attempts(
                max_llm_attempts, resolved_settings
            )
            self._validate_raw(raw_output, resolved_settings)
            if persist_raw_output or (
                raw_output_ref is not None and self._raw_store is not None
            ):
                await self._persist_raw(raw_output_ref, raw_output)
            return await self._run_pipeline(
                raw_output,
                schema=schema,
                settings=resolved_settings,
                mode=resolved_mode,
                allowances=resolved_allowances,
                provider=provider,
                api_key=api_key,
                max_llm_attempts=attempts_allowed,
                recorder=None,
                active_check=None,
            )
        except ProcessorError as exc:
            return RepairResult(
                ok=False,
                mode=resolved_mode,
                raw_output=raw_output if isinstance(raw_output, str) else "",
                error=exc,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("json repair task output processing failed")
            return RepairResult(
                ok=False,
                mode=resolved_mode,
                raw_output=raw_output if isinstance(raw_output, str) else "",
                error=ProcessorError(
                    ProcessorErrorCode.INTERNAL_ERROR,
                    "internal processor error",
                ),
            )

    async def process_job(
        self,
        job: Mapping[str, Any],
        lease_token: str,
        schema: Any = None,
        *,
        allowances: Iterable[ContentAllowance] | None = None,
        content_allowances: Iterable[ContentAllowance] | None = None,
        repair_mode: RepairMode | str | None = None,
        mode: RepairMode | str | None = None,
        settings: SettingsValue | None = None,
        max_llm_attempts: int | None = None,
    ) -> ProcessorOutcome:
        """Process one claimed repair job and finalize it with repository CAS.

        The returned :class:`ProcessorOutcome` has ``finalized=True`` when this
        service completed/failed/cancelled the job itself.  The worker must not
        finalize such an outcome again.  If the lease was lost, the result is
        not finalized so the worker's normal conflict accounting applies.
        """

        if not isinstance(job, Mapping):
            raise TypeError("job must be a mapping")
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.JOB_NOT_FOUND,
                "repair job id is missing",
            )
        if self._repository is None:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.NOT_CONFIGURED,
                "repair repository is not configured",
            )
        if not isinstance(lease_token, str) or not lease_token:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.LEASE_LOST,
                "repair job lease token is missing",
            )
        if content_allowances is not None:
            if allowances is not None:
                return ProcessorOutcome.failure(
                    ProcessorErrorCode.INVALID_INPUT,
                    "pass either allowances or content_allowances, not both",
                )
            allowances = content_allowances
        if mode is not None:
            if repair_mode is not None:
                return ProcessorOutcome.failure(
                    ProcessorErrorCode.INVALID_INPUT,
                    "pass either repair_mode or mode, not both",
                )
            repair_mode = mode

        lock = await self._lock_for(job_id)
        async with lock:
            try:
                return await self._process_job_locked(
                    dict(job),
                    job_id,
                    lease_token,
                    allowances=allowances,
                    repair_mode=repair_mode,
                    schema=schema,
                    settings=settings,
                    max_llm_attempts=max_llm_attempts,
                )
            except asyncio.CancelledError:
                raise
            except ProcessorError as exc:
                return await self._finalize_failure(
                    job_id,
                    lease_token,
                    exc,
                    RepairResult(
                        ok=False,
                        mode=RepairMode.SYNTAX_ONLY,
                        error=exc,
                    ),
                )
            except Exception:
                logger.error("json repair job %s failed unexpectedly", job_id)
                error = ProcessorError(
                    ProcessorErrorCode.INTERNAL_ERROR,
                    "internal processor error",
                )
                return await self._finalize_failure(
                    job_id,
                    lease_token,
                    error,
                    RepairResult(
                        ok=False,
                        mode=RepairMode.SYNTAX_ONLY,
                        error=error,
                    ),
                )

    # ------------------------------------------------------------------
    # Job processing internals
    # ------------------------------------------------------------------
    async def _process_job_locked(
        self,
        job: dict[str, Any],
        job_id: str,
        lease_token: str,
        *,
        allowances: Iterable[ContentAllowance] | None,
        repair_mode: RepairMode | str | None,
        schema: Any,
        settings: SettingsValue | None,
        max_llm_attempts: int | None,
    ) -> ProcessorOutcome:
        stored = await self._get_job(job_id)
        if stored is None:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.JOB_NOT_FOUND,
                "repair job not found",
            )
        terminal = self._outcome_from_stored(stored)
        if terminal is not None:
            return terminal
        if stored.get("status") != RepairJobStatus.PROCESSING.value:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.JOB_STATE_CONFLICT,
                "repair job is not processing",
            )
        if not _lease_is_active(stored, lease_token):
            return ProcessorOutcome.failure(
                ProcessorErrorCode.LEASE_LOST,
                "repair job lease is not owned or has expired",
            )
        if await self._is_cancelled(stored):
            return await self._cancel_outcome(job_id, lease_token)

        resolved_settings = await self._resolve_settings(settings)
        mode = self._resolve_mode(repair_mode, resolved_settings)
        resolved_allowances = await self._job_allowances(stored, allowances)
        resolved_schema = schema if schema is not None else stored.get("schema")
        raw_output = await self._load_raw(stored, job, resolved_settings)

        try:
            result = await self._run_pipeline(
                raw_output,
                schema=resolved_schema,
                settings=resolved_settings,
                mode=mode,
                allowances=resolved_allowances,
                provider=None,
                api_key=None,
                max_llm_attempts=self._resolve_max_attempts(
                    max_llm_attempts, resolved_settings
                ),
                recorder=self._attempt_recorder(job_id),
                active_check=lambda: self._active_error(job_id, lease_token),
            )
        except ProcessorError as exc:
            return await self._finalize_failure(
                job_id,
                lease_token,
                exc,
                RepairResult(ok=False, mode=mode, raw_output=raw_output, error=exc),
            )

        if result.ok:
            return await self._finalize_success(
                job_id,
                lease_token,
                result,
            )
        error = result.error or ProcessorError(
            ProcessorErrorCode.INTERNAL_ERROR,
            "repair pipeline failed without an error",
        )
        return await self._finalize_failure(
            job_id,
            lease_token,
            error,
            result,
        )

    async def _load_raw(
        self,
        stored: Mapping[str, Any],
        job_input: Mapping[str, Any],
        settings: JsonProcessorSettings,
    ) -> str:
        raw_output: Any = job_input.get("raw_output")
        if raw_output is None:
            ref = stored.get("raw_output_ref")
            if not isinstance(ref, str) or not ref:
                raise ProcessorError(
                    ProcessorErrorCode.INVALID_INPUT,
                    "repair job has no raw output reference",
                )
            if self._raw_loader is None:
                raise ProcessorError(
                    ProcessorErrorCode.NOT_CONFIGURED,
                    "raw output loader is not configured",
                )
            try:
                raw_output = await _invoke(self._raw_loader, ref)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProcessorError(
                    ProcessorErrorCode.INVALID_INPUT,
                    "raw output is unavailable",
                ) from None
        if not isinstance(raw_output, str):
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output must be a string",
            )
        self._validate_raw(raw_output, settings)
        try:
            digest = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output is not valid Unicode text",
            ) from None
        stored_hash = stored.get("raw_hash")
        if not isinstance(stored_hash, str) or digest != stored_hash:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output does not match the repair job",
            )
        if self._raw_store is not None:
            ref = stored.get("raw_output_ref")
            try:
                await _invoke(self._raw_store, str(ref or ""), raw_output)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProcessorError(
                    ProcessorErrorCode.INTERNAL_ERROR,
                    "raw output could not be stored",
                ) from None
        return raw_output

    async def _finalize_success(
        self,
        job_id: str,
        lease_token: str,
        result: RepairResult,
    ) -> ProcessorOutcome:
        active_error = await self._active_error(job_id, lease_token)
        if active_error is not None:
            return await self._outcome_for_active_error(
                job_id, lease_token, active_error
            )
        method = _method_for_attempts(result.attempts)
        try:
            stored = await _invoke(
                self._repository.complete_job,
                job_id,
                lease_owner=lease_token,
                result_json=result.value,
                method=method,
            )
        except (JsonRepairStateConflict, JsonRepairLeaseLost) as exc:
            return await self._outcome_after_race(job_id, exc)
        return ProcessorOutcome.success(
            _mapping_value(stored, "result", result.value),
            method=_coerce_method(
                _mapping_value(stored, "method", method) or method
            ),
            finalized=True,
        )

    async def _finalize_failure(
        self,
        job_id: str,
        lease_token: str,
        error: ProcessorError,
        result: RepairResult,
    ) -> ProcessorOutcome:
        if error.code is ProcessorErrorCode.CANCELLED:
            return await self._cancel_outcome(job_id, lease_token)

        active_error = await self._active_error(job_id, lease_token)
        if active_error is not None:
            return await self._outcome_for_active_error(
                job_id, lease_token, active_error
            )
        method = _method_for_attempts(result.attempts)
        try:
            stored = await _invoke(
                self._repository.fail_job,
                job_id,
                lease_owner=lease_token,
                error_code=error.code,
                error_message=_error_message(error),
                method=method,
            )
        except (JsonRepairStateConflict, JsonRepairLeaseLost) as exc:
            return await self._outcome_after_race(job_id, exc)
        return ProcessorOutcome.failure(
            error.code,
            _error_message(error),
            method=_coerce_method(
                _mapping_value(stored, "method", method) or method
            ),
            finalized=True,
        )

    async def _active_error(
        self,
        job_id: str,
        lease_token: str,
    ) -> ProcessorError | None:
        stored = await self._get_job(job_id)
        if stored is None:
            return ProcessorError(
                ProcessorErrorCode.JOB_NOT_FOUND,
                "repair job not found",
            )
        status = stored.get("status")
        if status == RepairJobStatus.CANCELLED.value:
            return ProcessorError(
                ProcessorErrorCode.CANCELLED,
                "repair job was cancelled",
            )
        if status in {
            RepairJobStatus.SUCCEEDED.value,
            RepairJobStatus.FAILED.value,
        }:
            return ProcessorError(
                ProcessorErrorCode.JOB_STATE_CONFLICT,
                f"repair job is already {status}",
            )
        if status != RepairJobStatus.PROCESSING.value:
            return ProcessorError(
                ProcessorErrorCode.JOB_STATE_CONFLICT,
                "repair job is not processing",
            )
        if not _lease_is_active(stored, lease_token):
            return ProcessorError(
                ProcessorErrorCode.LEASE_LOST,
                "repair job lease is not owned or has expired",
            )
        if await self._is_cancelled(stored):
            return ProcessorError(
                ProcessorErrorCode.CANCELLED,
                "repair job was cancelled",
            )
        return None

    async def _outcome_for_active_error(
        self,
        job_id: str,
        lease_token: str,
        error: ProcessorError,
    ) -> ProcessorOutcome:
        if error.code is ProcessorErrorCode.CANCELLED:
            return await self._cancel_outcome(job_id, lease_token)
        stored = await self._get_job(job_id)
        terminal = self._outcome_from_stored(stored) if stored is not None else None
        if terminal is not None:
            return terminal
        return ProcessorOutcome.failure(
            error.code,
            _error_message(error),
            finalized=False,
        )

    async def _outcome_after_race(
        self,
        job_id: str,
        exc: BaseException,
    ) -> ProcessorOutcome:
        stored = await self._get_job(job_id)
        terminal = self._outcome_from_stored(stored) if stored is not None else None
        if terminal is not None:
            return terminal
        code = getattr(exc, "code", ProcessorErrorCode.INTERNAL_ERROR)
        if not isinstance(code, ProcessorErrorCode):
            code = ProcessorErrorCode.INTERNAL_ERROR
        return ProcessorOutcome.failure(
            code,
            _safe_text(str(exc), fallback="repair job state changed"),
            finalized=False,
        )

    async def _cancel_outcome(
        self,
        job_id: str,
        lease_token: str,
    ) -> ProcessorOutcome:
        stored: Mapping[str, Any] | None = None
        try:
            stored = await _invoke(
                self._repository.cancel_job,
                job_id,
                lease_owner=lease_token,
                reason="cancelled",
            )
        except (JsonRepairStateConflict, JsonRepairLeaseLost, JsonRepairJobNotFound):
            stored = await self._get_job(job_id)
        except Exception:
            logger.warning("json repair job %s cancellation failed", job_id)
            stored = await self._get_job(job_id)
        if (
            _mapping_value(stored, "status") == RepairJobStatus.CANCELLED.value
        ):
            return ProcessorOutcome.failure(
                ProcessorErrorCode.CANCELLED,
                _safe_text(
                    _mapping_value(stored, "error_message"),
                    fallback="repair job was cancelled",
                ),
                finalized=True,
            )
        terminal = self._outcome_from_stored(stored) if stored is not None else None
        if terminal is not None:
            return terminal
        return ProcessorOutcome.failure(
            ProcessorErrorCode.CANCELLED,
            "repair job was cancelled",
            finalized=False,
        )

    @staticmethod
    def _outcome_from_stored(
        stored: Mapping[str, Any] | None,
    ) -> ProcessorOutcome | None:
        if not isinstance(stored, Mapping):
            return None
        status = stored.get("status")
        method = _coerce_method(stored.get("method"))
        if status == RepairJobStatus.SUCCEEDED.value:
            return ProcessorOutcome.success(
                stored.get("result"),
                method=method,
                finalized=True,
            )
        if status == RepairJobStatus.FAILED.value:
            return ProcessorOutcome.failure(
                stored.get("error_code") or ProcessorErrorCode.INTERNAL_ERROR,
                _safe_text(stored.get("error_message"), fallback="repair failed"),
                method=method,
                finalized=True,
            )
        if status == RepairJobStatus.CANCELLED.value:
            return ProcessorOutcome.failure(
                ProcessorErrorCode.CANCELLED,
                _safe_text(
                    stored.get("error_message"), fallback="repair job was cancelled"
                ),
                method=method,
                finalized=True,
            )
        return None

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------
    async def _run_pipeline(
        self,
        raw_output: str,
        *,
        schema: Any,
        settings: JsonProcessorSettings,
        mode: RepairMode,
        allowances: tuple[ContentAllowance, ...],
        provider: Any | None,
        api_key: str | None,
        max_llm_attempts: int,
        recorder: Callable[[_AttemptAudit], Awaitable[None]] | None,
        active_check: Callable[[], Awaitable[ProcessorError | None]] | None,
    ) -> RepairResult:
        attempts: list[RepairAttempt] = []
        number = 0

        def next_number() -> int:
            nonlocal number
            number += 1
            return number

        async def record(audit: _AttemptAudit) -> None:
            if recorder is not None:
                await recorder(audit)

        if schema is not None:
            try:
                validate_schema(schema)
            except SchemaDefinitionError as exc:
                return self._failure_result(
                    raw_output,
                    mode,
                    ProcessorError(
                        ProcessorErrorCode.SCHEMA_DEFINITION_INVALID,
                        exc.message,
                        exc.path,
                    ),
                    attempts,
                )

        strict_value, strict_ok = _strict_load(raw_output)
        baseline: Any = strict_value if strict_ok else _MISSING
        latest_text = (
            _canonical_json(strict_value) if strict_ok else raw_output
        ) or raw_output
        last_error: ProcessorError | None = None
        last_diagnostics: tuple[ValidationDiagnostic, ...] = ()

        if strict_ok:
            serializable_error = self._serializable_error(strict_value)
            if serializable_error is not None:
                return self._failure_result(
                    raw_output, mode, serializable_error, attempts
                )
            validation = validate_value(strict_value, schema)
            if validation.valid:
                return RepairResult(
                    ok=True,
                    mode=mode,
                    value=strict_value,
                    raw_output=raw_output,
                )
            last_error = validation.to_error()
            last_diagnostics = (
                last_error.diagnostics if last_error is not None else ()
            )

        if (not strict_ok) or settings.local_repair_enabled:
            active_error = await active_check() if active_check is not None else None
            if active_error is not None:
                return self._failure_result(
                    raw_output, mode, active_error, attempts
                )

            started = time.perf_counter()
            parsed = self._parse_output(
                raw_output,
                max_bytes=settings.max_input_bytes,
            )
            latency_ms = max(0, int((time.perf_counter() - started) * 1000))

            if parsed.ok:
                if baseline is _MISSING:
                    baseline = parsed.value
                validation = validate_value(parsed.value, schema)
                content = check_content_preserved(
                    baseline if baseline is not _MISSING else parsed.value,
                    parsed.value,
                    mode,
                    allowances=allowances,
                )
                serializable_error = self._serializable_error(parsed.value)
                local_ok = (
                    content.ok
                    and validation.valid
                    and serializable_error is None
                )
                if local_ok and settings.local_repair_enabled:
                    local_attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LOCAL,
                        mode,
                        raw_output,
                        parsed,
                        validation,
                    )
                    attempts.append(local_attempt)
                    await record(
                        self._audit_for_local(
                            local_attempt,
                            AttemptStatus.SUCCEEDED,
                            latency_ms=latency_ms,
                        )
                    )
                    return RepairResult(
                        ok=True,
                        mode=mode,
                        value=parsed.value,
                        raw_output=raw_output,
                        attempts=tuple(attempts),
                    )

                error = (
                    serializable_error
                    or content.error
                    or validation.to_error()
                    or ProcessorError(
                        ProcessorErrorCode.NOT_REPAIRABLE,
                        "local repair is disabled",
                    )
                )
                if settings.local_repair_enabled:
                    local_attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LOCAL,
                        mode,
                        raw_output,
                        parsed,
                        validation,
                        error,
                    )
                    attempts.append(local_attempt)
                    await record(
                        self._audit_for_local(
                            local_attempt,
                            AttemptStatus.FAILED,
                            latency_ms=latency_ms,
                        )
                    )
                if content.ok:
                    latest_text = _canonical_json(parsed.value) or latest_text
                last_error = error
                last_diagnostics = _diagnostics_for(error)
            else:
                error = parsed.error or ProcessorError(
                    ProcessorErrorCode.NOT_REPAIRABLE,
                    "raw output is not valid JSON",
                )
                if settings.local_repair_enabled:
                    local_attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LOCAL,
                        mode,
                        raw_output,
                        parsed,
                        None,
                        error,
                    )
                    attempts.append(local_attempt)
                    await record(
                        self._audit_for_local(
                            local_attempt,
                            AttemptStatus.FAILED,
                            latency_ms=latency_ms,
                        )
                    )
                last_error = error
                last_diagnostics = _diagnostics_for(error)

        if max_llm_attempts <= 0:
            final = last_error or ProcessorError(
                ProcessorErrorCode.NOT_REPAIRABLE,
                "raw output is not valid JSON",
            )
            return self._failure_result(raw_output, mode, final, attempts)

        try:
            active_provider, owned_provider = await self._resolve_provider(
                settings, provider
            )
        except ProcessorError as exc:
            return self._failure_result(raw_output, mode, exc, attempts)

        resolved_key: str | None = None
        try:
            try:
                resolved_key = await self._resolve_api_key(settings, api_key)
            except ProcessorError as exc:
                return self._failure_result(raw_output, mode, exc, attempts)
            for _ in range(max_llm_attempts):
                active_error = (
                    await active_check() if active_check is not None else None
                )
                if active_error is not None:
                    return self._failure_result(
                        raw_output, mode, active_error, attempts
                    )

                try:
                    prompt = build_repair_prompt(
                        latest_text,
                        schema=schema,
                        diagnostics=last_diagnostics,
                        mode=mode,
                    )
                except ProcessorError as exc:
                    return self._failure_result(
                        raw_output, mode, exc, attempts
                    )

                prompt_meta: dict[str, Any] = {
                    "phase": "llm",
                    "mode": mode.value,
                    "prompt_bytes": prompt.byte_length,
                    "prompt_truncated": prompt.truncated,
                    "raw_truncated": prompt.raw_truncated,
                    "schema_truncated": prompt.schema_truncated,
                    "diagnostics_truncated": prompt.diagnostics_truncated,
                    "candidate_bytes": len(latest_text.encode("utf-8")),
                }
                started = time.perf_counter()
                try:
                    completion = await asyncio.wait_for(
                        _invoke(
                            active_provider.complete,
                            [
                                {"role": "system", "content": prompt.system},
                                {"role": "user", "content": prompt.user},
                            ],
                            api_key=resolved_key,
                        ),
                        timeout=float(settings.request_timeout_seconds) + 1.0,
                    )
                except asyncio.CancelledError:
                    raise
                except ProviderError as exc:
                    code = (
                        ProcessorErrorCode.NOT_CONFIGURED
                        if exc.kind is ProviderErrorKind.CONFIGURATION
                        else exc.processor_code
                    )
                    error = ProcessorError(
                        code,
                        _redact_secret(exc.message, resolved_key),
                    )
                    attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LLM,
                        mode,
                        "",
                        ParseResult.failure(error, raw_output=""),
                        None,
                        error,
                    )
                    attempts.append(attempt)
                    await record(
                        self._audit_for_llm(
                            attempt,
                            AttemptStatus.FAILED,
                            active_provider,
                            settings,
                            latency_ms=max(
                                0,
                                int((time.perf_counter() - started) * 1000),
                            ),
                            request_meta={
                                **prompt_meta,
                                "error_kind": exc.kind.value,
                                "status_code": exc.status_code,
                                "provider_code": exc.provider_code,
                                "infrastructure_retries": exc.infrastructure_retries,
                            },
                        )
                    )
                    return self._failure_result(
                        raw_output, mode, error, attempts
                    )
                except TimeoutError:
                    error = ProcessorError(
                        ProcessorErrorCode.TIMEOUT,
                        "provider request timed out",
                    )
                    attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LLM,
                        mode,
                        "",
                        ParseResult.failure(error, raw_output=""),
                        None,
                        error,
                    )
                    attempts.append(attempt)
                    await record(
                        self._audit_for_llm(
                            attempt,
                            AttemptStatus.FAILED,
                            active_provider,
                            settings,
                            latency_ms=max(
                                0,
                                int((time.perf_counter() - started) * 1000),
                            ),
                            request_meta={
                                **prompt_meta,
                                "error_kind": "timeout",
                            },
                        )
                    )
                    return self._failure_result(
                        raw_output, mode, error, attempts
                    )
                except Exception:
                    error = ProcessorError(
                        ProcessorErrorCode.PROVIDER_ERROR,
                        "provider request failed",
                    )
                    attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LLM,
                        mode,
                        "",
                        ParseResult.failure(error, raw_output=""),
                        None,
                        error,
                    )
                    attempts.append(attempt)
                    await record(
                        self._audit_for_llm(
                            attempt,
                            AttemptStatus.FAILED,
                            active_provider,
                            settings,
                            latency_ms=max(
                                0,
                                int((time.perf_counter() - started) * 1000),
                            ),
                            request_meta={
                                **prompt_meta,
                                "error_kind": "unexpected",
                            },
                        )
                    )
                    return self._failure_result(
                        raw_output, mode, error, attempts
                    )

                latency_ms = _completion_field(completion, "latency_ms")
                if (
                    isinstance(latency_ms, bool)
                    or not isinstance(latency_ms, int)
                    or latency_ms < 0
                ):
                    latency_ms = max(
                        0, int((time.perf_counter() - started) * 1000)
                    )
                content_text = (
                    completion
                    if isinstance(completion, str)
                    else _completion_field(completion, "content", "")
                )
                if not isinstance(content_text, str):
                    content_text = ""

                active_error = (
                    await active_check() if active_check is not None else None
                )
                if active_error is not None:
                    attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LLM,
                        mode,
                        content_text,
                        ParseResult.failure(active_error, raw_output=content_text),
                        None,
                        active_error,
                    )
                    attempts.append(attempt)
                    await record(
                        self._audit_for_llm(
                            attempt,
                            (
                                AttemptStatus.CANCELLED
                                if active_error.code
                                is ProcessorErrorCode.CANCELLED
                                else AttemptStatus.FAILED
                            ),
                            active_provider,
                            settings,
                            latency_ms=latency_ms,
                            request_meta={
                                **prompt_meta,
                                "error_kind": active_error.code.value,
                            },
                            completion=completion,
                        )
                    )
                    return self._failure_result(
                        raw_output, mode, active_error, attempts
                    )

                parsed = self._parse_output(
                    content_text,
                    max_bytes=settings.max_output_bytes,
                )
                validation = (
                    validate_value(parsed.value, schema) if parsed.ok else None
                )
                content_error: ProcessorError | None = None
                if parsed.ok:
                    if baseline is _MISSING:
                        baseline = parsed.value
                    check = check_content_preserved(
                        baseline,
                        parsed.value,
                        mode,
                        allowances=allowances,
                    )
                    if not check.ok:
                        content_error = check.error
                serializable_error = (
                    self._serializable_error(parsed.value)
                    if parsed.ok
                    else None
                )
                if (
                    parsed.ok
                    and content_error is None
                    and serializable_error is None
                    and (validation is None or validation.valid)
                ):
                    attempt = RepairAttempt(
                        next_number(),
                        AttemptPhase.LLM,
                        mode,
                        content_text,
                        parsed,
                        validation,
                    )
                    attempts.append(attempt)
                    await record(
                        self._audit_for_llm(
                            attempt,
                            AttemptStatus.SUCCEEDED,
                            active_provider,
                            settings,
                            latency_ms=latency_ms,
                            request_meta=prompt_meta,
                            completion=completion,
                        )
                    )
                    return RepairResult(
                        ok=True,
                        mode=mode,
                        value=parsed.value,
                        raw_output=raw_output,
                        attempts=tuple(attempts),
                    )

                error = (
                    serializable_error
                    or content_error
                    or (validation.to_error() if validation is not None else None)
                    or parsed.error
                    or ProcessorError(
                        ProcessorErrorCode.NOT_REPAIRABLE,
                        "provider output is not valid JSON",
                    )
                )
                attempt = RepairAttempt(
                    next_number(),
                    AttemptPhase.LLM,
                    mode,
                    content_text,
                    parsed,
                    validation,
                    error,
                )
                attempts.append(attempt)
                await record(
                    self._audit_for_llm(
                        attempt,
                        AttemptStatus.FAILED,
                        active_provider,
                        settings,
                        latency_ms=latency_ms,
                        request_meta=prompt_meta,
                        completion=completion,
                    )
                )
                if error.code is ProcessorErrorCode.CONTENT_CHANGED:
                    return self._failure_result(
                        raw_output, mode, error, attempts
                    )
                last_error = error
                last_diagnostics = _diagnostics_for(error)
                latest_text = (
                    _canonical_json(parsed.value)
                    if parsed.ok
                    else content_text
                ) or latest_text
        finally:
            if owned_provider:
                close = getattr(active_provider, "aclose", None)
                if callable(close):
                    try:
                        await _invoke(close)
                    except Exception:
                        logger.warning("json repair provider close failed")

        final = last_error or ProcessorError(
            ProcessorErrorCode.NOT_REPAIRABLE,
            "raw output is not valid JSON",
        )
        return self._failure_result(raw_output, mode, final, attempts)

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------
    def _parse_output(self, raw_output: str, *, max_bytes: int) -> ParseResult:
        """Hook point for tests and future parser versions."""

        return parse_json_output(raw_output, max_bytes=max_bytes)

    @staticmethod
    def _serializable_error(value: Any) -> ProcessorError | None:
        if _canonical_json(value) is None:
            return ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "JSON value contains a non-finite number",
            )
        return None

    @staticmethod
    def _failure_result(
        raw_output: str,
        mode: RepairMode,
        error: ProcessorError,
        attempts: list[RepairAttempt],
    ) -> RepairResult:
        return RepairResult(
            ok=False,
            mode=mode,
            raw_output=raw_output,
            attempts=tuple(attempts),
            error=error,
        )

    @staticmethod
    def _audit_for_local(
        attempt: RepairAttempt,
        status: AttemptStatus,
        *,
        latency_ms: int | None,
    ) -> _AttemptAudit:
        return _AttemptAudit(
            attempt=attempt,
            method=RepairMethod.LOCAL,
            status=status,
            request_meta={
                "phase": "local",
                "strategy": attempt.parse.strategy.value,
                "normalizations": list(attempt.parse.normalizations),
                "candidate_count": attempt.parse.candidate_count,
            },
            validation_error=(
                _validation_text(attempt.error)
                if attempt.error is not None
                else None
            ),
            latency_ms=latency_ms,
        )

    def _audit_for_llm(
        self,
        attempt: RepairAttempt,
        status: AttemptStatus,
        provider: Any,
        settings: JsonProcessorSettings,
        *,
        latency_ms: int | None,
        request_meta: Mapping[str, Any] | None = None,
        completion: Any | None = None,
    ) -> _AttemptAudit:
        meta: dict[str, Any] = {
            "phase": "llm",
            "mode": attempt.mode.value,
            "candidate_count": attempt.parse.candidate_count,
        }
        if request_meta is not None:
            meta.update(request_meta)
        if attempt.validation is not None:
            meta["schema_valid"] = attempt.validation.valid

        default_provider_name = (
            "openai-compatible"
            if isinstance(provider, OpenAICompatibleProvider)
            else type(provider).__name__
        )
        provider_name = (
            self._provider_name.strip()
            if isinstance(self._provider_name, str) and self._provider_name.strip()
            else default_provider_name
        )[:200]
        model = (
            _completion_field(completion, "model")
            if completion is not None
            else None
        )
        if not isinstance(model, str) or not model:
            model = getattr(provider, "model", None) or settings.model or None
        token_usage: Mapping[str, int] | None = None
        if completion is not None:
            usage = _completion_field(completion, "usage")
            if isinstance(usage, Mapping):
                token_usage = {
                    key: value
                    for key, value in usage.items()
                    if isinstance(key, str)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                }
            for key in (
                "status_code",
                "response_format_used",
                "infrastructure_retries",
            ):
                value = _completion_field(completion, key)
                if isinstance(value, (int, bool)):
                    meta[key] = value
            fallback = _completion_field(
                completion, "response_format_fallback_used"
            )
            if isinstance(fallback, bool):
                meta["response_format_fallback_used"] = fallback
        return _AttemptAudit(
            attempt=attempt,
            method=RepairMethod.LLM,
            status=status,
            provider=provider_name,
            model=(
                model.strip()[:200]
                if isinstance(model, str) and model.strip()
                else None
            ),
            request_meta=meta,
            validation_error=(
                _validation_text(attempt.error)
                if attempt.error is not None
                else None
            ),
            latency_ms=latency_ms,
            token_usage=token_usage,
        )

    # ------------------------------------------------------------------
    # Settings, provider, and raw-output wiring
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_settings(value: SettingsValue) -> JsonProcessorSettings:
        if isinstance(value, JsonProcessorSettings):
            return value
        if isinstance(value, Mapping):
            try:
                return JsonProcessorSettings.from_dict(value, reject_unknown=False)
            except SettingsValidationError as exc:
                raise ProcessorError(
                    ProcessorErrorCode.SETTINGS_INVALID,
                    _safe_text(str(exc), fallback="processor settings are invalid"),
                ) from None
        raise ProcessorError(
            ProcessorErrorCode.SETTINGS_INVALID,
            "processor settings must be a JsonProcessorSettings or object",
        )

    async def _resolve_settings(
        self,
        override: SettingsValue | None,
    ) -> JsonProcessorSettings:
        if override is not None:
            return self._coerce_settings(override)
        if self._settings is not None:
            return self._settings
        if self._settings_loader is not None:
            try:
                value = await _invoke(self._settings_loader)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProcessorError(
                    ProcessorErrorCode.SETTINGS_INVALID,
                    "processor settings could not be loaded",
                ) from None
            return self._coerce_settings(value)
        if self._repository is not None:
            try:
                get_model = getattr(
                    self._repository, "get_settings_model", None
                )
                if callable(get_model):
                    return self._coerce_settings(await _invoke(get_model))
                return self._coerce_settings(
                    await _invoke(self._repository.get_settings)
                )
            except SettingsValidationError as exc:
                raise ProcessorError(
                    ProcessorErrorCode.SETTINGS_INVALID,
                    _safe_text(str(exc), fallback="processor settings are invalid"),
                ) from None
            except asyncio.CancelledError:
                raise
            except ProcessorError:
                raise
            except Exception:
                raise ProcessorError(
                    ProcessorErrorCode.SETTINGS_INVALID,
                    "processor settings could not be loaded",
                ) from None
        return JsonProcessorSettings()

    @staticmethod
    def _resolve_mode(
        value: RepairMode | str | None,
        settings: JsonProcessorSettings,
    ) -> RepairMode:
        resolved: Any = settings.repair_mode if value is None else value
        if isinstance(resolved, RepairMode):
            return resolved
        try:
            return RepairMode(resolved)
        except ValueError:
            raise ProcessorError(
                ProcessorErrorCode.SETTINGS_INVALID,
                "repair_mode must be syntax_only or schema_safe",
            ) from None

    def _resolve_allowances(
        self,
        allowances: Iterable[ContentAllowance] | None,
    ) -> tuple[ContentAllowance, ...]:
        if allowances is None:
            return self._allowances
        if isinstance(allowances, ContentAllowance):
            return (allowances,)
        try:
            items = tuple(allowances)
        except TypeError:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "content allowances must be iterable",
            ) from None
        if any(not isinstance(item, ContentAllowance) for item in items):
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "content allowances are invalid",
            )
        return items

    async def _job_allowances(
        self,
        job: Mapping[str, Any],
        allowances: Iterable[ContentAllowance] | None,
    ) -> tuple[ContentAllowance, ...]:
        if allowances is not None:
            return self._resolve_allowances(allowances)
        if self._allowance_provider is None:
            return self._allowances
        try:
            provided = await _invoke(self._allowance_provider, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "content allowances could not be resolved",
            ) from None
        return self._resolve_allowances(provided)

    @staticmethod
    def _resolve_max_attempts(
        value: int | None,
        settings: JsonProcessorSettings,
    ) -> int:
        resolved = settings.max_llm_attempts if value is None else value
        if isinstance(resolved, bool) or not isinstance(resolved, int):
            raise ProcessorError(
                ProcessorErrorCode.SETTINGS_INVALID,
                "max_llm_attempts must be an integer",
            )
        if not 0 <= resolved <= 3:
            raise ProcessorError(
                ProcessorErrorCode.SETTINGS_INVALID,
                "max_llm_attempts must be between 0 and 3",
            )
        return resolved

    @staticmethod
    def _validate_raw(
        raw_output: Any,
        settings: JsonProcessorSettings,
    ) -> None:
        if not isinstance(raw_output, str):
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output must be a string",
            )
        try:
            size = len(raw_output.encode("utf-8"))
        except UnicodeEncodeError:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output is not valid Unicode text",
            ) from None
        if size > settings.max_input_bytes:
            raise ProcessorError(
                ProcessorErrorCode.RAW_OUTPUT_TOO_LARGE,
                f"raw output exceeds {settings.max_input_bytes} bytes",
            )

    async def _persist_raw(
        self,
        raw_output_ref: str | None,
        raw_output: str,
    ) -> None:
        if self._raw_store is None:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "raw output store is not configured",
            )
        if not isinstance(raw_output_ref, str) or not raw_output_ref:
            raise ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output reference is required",
            )
        try:
            await _invoke(self._raw_store, raw_output_ref, raw_output)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProcessorError(
                ProcessorErrorCode.INTERNAL_ERROR,
                "raw output could not be stored",
            ) from None

    async def _resolve_provider(
        self,
        settings: JsonProcessorSettings,
        override: Any | None,
    ) -> tuple[Any, bool]:
        if override is not None:
            return override, False
        if self._provider is not None:
            return self._provider, False
        if self._provider_factory is not None:
            try:
                provider = await _invoke(self._provider_factory, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProcessorError(
                    ProcessorErrorCode.NOT_CONFIGURED,
                    "provider configuration is invalid",
                ) from None
            if provider is None:
                raise ProcessorError(
                    ProcessorErrorCode.NOT_CONFIGURED,
                    "provider is not configured",
                )
            return provider, True
        if not settings.enabled:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "JSON repair provider is disabled",
            )
        if not settings.base_url:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "provider base_url is not configured",
            )
        if not settings.model:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "provider model is not configured",
            )
        try:
            provider = OpenAICompatibleProvider(
                settings,
                allow_private_networks=self._allow_private_networks,
            )
        except ProviderConfigurationError:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "provider configuration is invalid",
            ) from None
        return provider, True

    async def _resolve_api_key(
        self,
        settings: JsonProcessorSettings,
        override: str | None,
    ) -> str | None:
        if override is not None:
            if not isinstance(override, str):
                raise ProcessorError(
                    ProcessorErrorCode.SETTINGS_INVALID,
                    "api key must be a string",
                )
            return override
        if self._api_key is not None:
            return self._api_key
        if self._api_key_resolver is None or not settings.api_key_ref:
            return None
        try:
            value = await _invoke(self._api_key_resolver, settings.api_key_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProcessorError(
                ProcessorErrorCode.NOT_CONFIGURED,
                "provider credentials are unavailable",
            ) from None
        if value is None or isinstance(value, str):
            return value
        raise ProcessorError(
            ProcessorErrorCode.NOT_CONFIGURED,
            "provider credentials are unavailable",
        )

    # ------------------------------------------------------------------
    # Repository and cancellation helpers
    # ------------------------------------------------------------------
    async def _get_job(self, job_id: str) -> dict[str, Any] | None:
        if self._repository is None:
            return None
        return await _invoke(self._repository.get_job, job_id)

    async def _is_cancelled(self, job: Mapping[str, Any]) -> bool:
        if self._cancellation_check is None:
            return False
        try:
            try:
                value = await _invoke(self._cancellation_check, job)
            except TypeError:
                # Accept both ``lambda job: ...`` and the simpler
                # ``lambda: ...`` hook used by tests/integrations.
                value = await _invoke(self._cancellation_check)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProcessorError(
                ProcessorErrorCode.INTERNAL_ERROR,
                "cancellation check failed",
            ) from None
        return bool(value)

    def _attempt_recorder(
        self,
        job_id: str,
    ) -> Callable[[_AttemptAudit], Awaitable[None]]:
        async def record(audit: _AttemptAudit) -> None:
            if self._repository is None:
                return
            await _invoke(
                self._repository.add_attempt,
                job_id,
                method=audit.method,
                provider=audit.provider,
                model=audit.model,
                request_meta=dict(audit.request_meta),
                validation_error=audit.validation_error,
                status=audit.status,
                latency_ms=audit.latency_ms,
                token_usage=(
                    dict(audit.token_usage)
                    if audit.token_usage is not None
                    else None
                ),
            )

        return record

    async def _lock_for(self, job_id: str) -> asyncio.Lock:
        async with self._job_locks_guard:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = asyncio.Lock()
                self._job_locks[job_id] = lock
            return lock


# Compatibility alias matching the package's ``JsonProcessor*`` naming.
JsonProcessorService = JsonRepairService

__all__ = ["JsonProcessorService", "JsonRepairService"]
