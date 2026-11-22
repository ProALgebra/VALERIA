"""M8 integration helpers for the standalone JSON repair processor.

This module is the only place that couples the processor to VALERIA Task/VFlow
persistence.  The pure core, repository, worker, and service remain independent
of VFlow.  VFlow uses :class:`JsonProcessorVFlowAdapter` only to create and poll
correlated repair jobs; the actual parse/local/LLM work is performed by the
separate :class:`~opencode_web.json_processor.worker.JsonRepairWorker` started
from the application lifespan.

Security notes:
- ``api_key_ref`` is resolved through an environment variable.  The reference
  (and never the key) is persisted; keys are read in memory for one provider
  call and are never returned by an API or written to an event/log.
- Raw Task output is already stored in ``task_run_results``.  The worker loads
  it by opaque reference (the Task run id) and verifies its SHA-256 against the
  job before processing.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

from ..database import Database
from .models import ProcessorErrorCode, SchemaDefinitionError
from .repository import JsonProcessorRepository
from .schema import validate_schema
from .service import JsonRepairService
from .settings import SettingsValidationError
from .worker import JsonRepairWorker

# Bump when the repair prompt/contract changes in a way that must invalidate
# canonical job identity.
JSON_PROCESSOR_PROMPT_VERSION = "task-output-v1"
_ENV_REF_RE = re.compile(r"^(?:env:)?([A-Za-z_][A-Za-z0-9_]*)$")


class JsonProcessorIntegrationError(RuntimeError):
    """A safe integration failure with a stable processor error code."""

    def __init__(
        self,
        code: ProcessorErrorCode | str,
        message: str,
    ) -> None:
        self.code = (
            code if isinstance(code, ProcessorErrorCode) else ProcessorErrorCode(code)
        )
        self.message = message
        super().__init__(message)


def resolve_env_api_key_ref(
    reference: str,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an ``api_key_ref`` as an environment variable name.

    Accepted forms are ``NAME`` and ``env:NAME``.  An empty/unknown reference
    returns ``None``; malformed references are rejected without echoing the
    value.  This is intentionally a local/operator configuration mechanism, not
    a secret store.
    """

    if not isinstance(reference, str):
        return None
    normalized = reference.strip()
    if not normalized:
        return None
    match = _ENV_REF_RE.fullmatch(normalized)
    if match is None:
        return None
    value = (os.environ if environ is None else environ).get(match.group(1))
    return value if isinstance(value, str) and value else None


def task_result_raw_output_loader(database: Database):
    """Load the immutable final Task text recorded by the queue dispatcher."""

    def load(reference: str) -> str:
        result = database.get_task_result(reference)
        text = result.get("text") if result else None
        if not isinstance(text, str):
            raise ValueError("Task result text is unavailable")
        return text

    return load


def vflow_run_cancellation_check(database: Database):
    """Cancel a processor job whose VFlow Run is no longer running."""

    def cancelled(job: Mapping[str, Any]) -> bool:
        run_id = job.get("vflow_run_id")
        if not isinstance(run_id, str) or not run_id:
            return False
        run = database._one("SELECT status FROM vflow_runs WHERE id = ?", (run_id,))
        return bool(
            run
            and run["status"] in {"cancel_requested", "cancelled", "failed"}
        )

    return cancelled


class JsonProcessorVFlowAdapter:
    """Synchronous bridge used by the VFlow runtime transaction.

    Every method is a short SQLite operation.  No provider call happens here;
    it is the worker that performs parsing and LLM repair outside the VFlow
    scheduler thread.  ``ready()`` is fail-closed: a disabled or incomplete
    processor cannot silently bypass validation.
    """

    def __init__(self, repository: JsonProcessorRepository) -> None:
        self.repository = repository

    def _settings(self) -> Mapping[str, Any]:
        return self.repository.get_settings()

    def ready(self) -> bool:
        try:
            settings = self._settings()
        except Exception:
            return False
        if not bool(settings.get("enabled")):
            return False
        if bool(settings.get("local_repair_enabled")):
            return True
        return bool(
            int(settings.get("max_llm_attempts") or 0) > 0
            and settings.get("base_url")
            and settings.get("model")
        )

    def unavailable_reason(self) -> str:
        try:
            settings = self._settings()
        except Exception:
            return "JSON repair processor settings are unavailable"
        if not bool(settings.get("enabled")):
            return "JSON repair processor is disabled"
        if not bool(settings.get("local_repair_enabled")) and not (
            int(settings.get("max_llm_attempts") or 0) > 0
            and settings.get("base_url")
            and settings.get("model")
        ):
            return "JSON repair processor has no repair path configured"
        return "JSON repair processor is not configured"

    def create_repair_job(
        self,
        *,
        raw_output: str,
        raw_output_ref: str,
        schema: Any,
        task_run_id: str | None,
        vflow_run_id: str,
        point_id: str,
    ) -> dict[str, Any]:
        if not self.ready():
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.NOT_CONFIGURED, self.unavailable_reason()
            )
        if not isinstance(raw_output, str):
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.INVALID_INPUT, "Task output must be text"
            )
        if not isinstance(raw_output_ref, str) or not raw_output_ref:
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.INVALID_INPUT, "Task output reference is missing"
            )
        resolved_schema = {} if schema is None else schema
        if not isinstance(resolved_schema, dict):
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.SCHEMA_DEFINITION_INVALID,
                "Task output schema must be a JSON object",
            )
        try:
            validate_schema(resolved_schema)
        except SchemaDefinitionError as exc:
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.SCHEMA_DEFINITION_INVALID,
                f"Task output schema is invalid: {exc}",
            ) from None
        raw_hash = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
        settings = self._settings()
        try:
            return self.repository.create_or_get_job(
                source_type="vflow",
                raw_output_ref=raw_output_ref,
                raw_hash=raw_hash,
                schema=resolved_schema,
                prompt_version=JSON_PROCESSOR_PROMPT_VERSION,
                task_run_id=task_run_id,
                vflow_run_id=vflow_run_id,
                point_id=point_id,
                settings_version=int(settings["version"]),
            )
        except ValueError as exc:
            raise JsonProcessorIntegrationError(
                ProcessorErrorCode.INVALID_INPUT, str(exc)
            ) from None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.repository.get_job(job_id)

    def cancel_run_jobs(self, vflow_run_id: str) -> int:
        jobs = self.repository.cancel_jobs_for_run(
            vflow_run_id=vflow_run_id,
            force=True,
            reason="vflow run cancelled",
        )
        return len(jobs)


def build_worker(
    database: Database,
    *,
    allow_private_networks: bool = False,
) -> tuple[JsonProcessorRepository, JsonRepairService, JsonRepairWorker]:
    """Build a configured worker stack or raise a safe configuration error."""

    repository = JsonProcessorRepository(database)
    try:
        settings = repository.get_settings_model()
    except SettingsValidationError as exc:
        raise JsonProcessorIntegrationError(
            ProcessorErrorCode.NOT_CONFIGURED,
            f"JSON repair processor settings are invalid: {exc}",
        ) from None
    if not settings.enabled:
        raise JsonProcessorIntegrationError(
            ProcessorErrorCode.NOT_CONFIGURED,
            "JSON repair processor is disabled",
        )
    if (
        not settings.local_repair_enabled
        and settings.max_llm_attempts == 0
    ):
        raise JsonProcessorIntegrationError(
            ProcessorErrorCode.NOT_CONFIGURED,
            "JSON repair processor has no repair path configured",
        )
    service = JsonRepairService(
        repository,
        settings_loader=repository.get_settings_model,
        api_key_resolver=resolve_env_api_key_ref,
        raw_output_loader=task_result_raw_output_loader(database),
        cancellation_check=vflow_run_cancellation_check(database),
        allow_private_networks=allow_private_networks,
    )
    worker = JsonRepairWorker(
        repository,
        service,
        concurrency_limit=settings.concurrency_limit,
        queue_limit=settings.queue_limit,
    )
    return repository, service, worker


__all__ = [
    "JSON_PROCESSOR_PROMPT_VERSION",
    "JsonProcessorIntegrationError",
    "JsonProcessorVFlowAdapter",
    "build_worker",
    "resolve_env_api_key_ref",
    "task_result_raw_output_loader",
    "vflow_run_cancellation_check",
]
