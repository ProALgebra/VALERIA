from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opencode_web.database import Database
from opencode_web.json_processor import (
    ParseResult,
    ParseStrategy,
    ProcessorError,
    ProcessorErrorCode,
    RepairJobStatus,
    RepairMethod,
    RepairMode,
    RepairSourceType,
    placeholder_allowance,
)
from opencode_web.json_processor.provider import (
    ProviderError,
    ProviderErrorKind,
)
from opencode_web.json_processor.repository import JsonProcessorRepository
from opencode_web.json_processor.service import JsonRepairService
from opencode_web.json_processor.settings import JsonProcessorSettings
from opencode_web.json_processor.worker import JsonRepairWorker

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}
STATUS_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"enum": ["ok"]}},
    "additionalProperties": False,
}
NOTE_SCHEMA = {
    "type": "object",
    "required": ["name", "note"],
    "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class FakeCompletion:
    content: str
    model: str = "fake-model"
    usage: dict[str, int] | None = None
    status_code: int = 200
    latency_ms: int = 1
    response_format_used: bool = True
    infrastructure_retries: int = 0


class FakeProvider:
    """Deterministic provider double.  Never performs network I/O."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    async def complete(self, messages: Any, **kwargs: Any) -> FakeCompletion:
        self.calls.append({"messages": messages, **kwargs})
        if not self.responses:
            raise AssertionError("provider was called unexpectedly")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeCompletion):
            return item
        return FakeCompletion(
            str(item),
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        )

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "json-processor-service.sqlite3", "s" * 32)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def repository(database: Database) -> JsonProcessorRepository:
    return JsonProcessorRepository(database)


def raw_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_job(
    repository: JsonProcessorRepository,
    raw: str,
    *,
    schema: dict[str, Any] | None = SCHEMA,
    task_run_id: str = "task-run-service",
    ref: str = "memory://raw/1",
) -> dict[str, Any]:
    return repository.create_or_get_job(
        source_type=RepairSourceType.TASK,
        task_run_id=task_run_id,
        raw_output_ref=ref,
        raw_hash=raw_hash(raw),
        schema=SCHEMA if schema is None else schema,
        prompt_version="v1",
    )


def claim(repository: JsonProcessorRepository, owner: str = "service-owner") -> dict:
    jobs = repository.claim_due(1, lease_seconds=60, owner=owner)
    assert len(jobs) == 1
    return jobs[0]


def llm_settings(**overrides: Any) -> JsonProcessorSettings:
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://provider.test",
        "model": "repair-model",
        "max_llm_attempts": 2,
    }
    data.update(overrides)
    return JsonProcessorSettings.from_dict(data)


def local_only_settings(**overrides: Any) -> JsonProcessorSettings:
    data: dict[str, Any] = {
        "enabled": False,
        "local_repair_enabled": True,
        "max_llm_attempts": 0,
    }
    data.update(overrides)
    return JsonProcessorSettings.from_dict(data)


def make_service(
    repository: JsonProcessorRepository,
    *,
    raw: str,
    provider: Any | None = None,
    settings: JsonProcessorSettings | None = None,
    **kwargs: Any,
) -> JsonRepairService:
    return JsonRepairService(
        repository,
        provider=provider,
        settings=settings,
        raw_output_loader=kwargs.pop("raw_output_loader", lambda ref: raw),
        **kwargs,
    )


def attempt_rows(repository: JsonProcessorRepository, job_id: str) -> list[dict]:
    return repository.list_attempts(job_id)


# ---------------------------------------------------------------------------
# Strict parse and local repair
# ---------------------------------------------------------------------------


async def test_valid_json_succeeds_without_provider_call(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider()
    service = make_service(repository, raw=raw, provider=provider)

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is True
    assert outcome.method is RepairMethod.NONE
    assert outcome.finalized is True
    assert outcome.result == {"ok": True}
    assert provider.calls == []
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == RepairJobStatus.SUCCEEDED.value
    assert stored["method"] == RepairMethod.NONE.value
    assert stored["result"] == {"ok": True}
    assert attempt_rows(repository, job["id"]) == []


async def test_local_syntax_repair_is_content_preserving(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true,}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider()
    service = make_service(repository, raw=raw, provider=provider)

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is True
    assert outcome.method is RepairMethod.LOCAL
    assert provider.calls == []
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["method"] == "local"
    assert stored["result"] == {"ok": True}
    assert stored["raw_hash"] == raw_hash(raw)
    attempts = attempt_rows(repository, job["id"])
    assert len(attempts) == 1
    assert attempts[0]["method"] == "local"
    assert attempts[0]["status"] == "succeeded"
    assert "trailing_commas" in attempts[0]["request_meta"]["normalizations"]


async def test_local_repair_can_be_disabled_and_llm_gets_the_candidate(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true,}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider(['{"ok": true}'])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(local_repair_enabled=False),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is True
    assert outcome.method is RepairMethod.LLM
    assert len(provider.calls) == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["method"] == "llm"
    attempts = attempt_rows(repository, job["id"])
    assert len(attempts) == 1
    assert attempts[0]["method"] == "llm"
    assert attempts[0]["status"] == "succeeded"


async def test_local_repair_can_be_disabled_and_fail_closed(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true,}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    service = make_service(
        repository,
        raw=raw,
        settings=local_only_settings(local_repair_enabled=False),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.NOT_REPAIRABLE
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["result_json"] is None
    assert attempt_rows(repository, job["id"]) == []


async def test_unsafe_local_candidate_is_rejected_before_llm(
    repository: JsonProcessorRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"ok": true}'
    schema = {
        "type": "object",
        "required": ["ok", "count"],
        "properties": {
            "ok": {"type": "boolean"},
            "count": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    job = make_job(repository, raw, schema=schema)
    claimed = claim(repository)
    service = make_service(
        repository,
        raw=raw,
        settings=local_only_settings(),
    )

    def unsafe_parse(raw_output: str, *, max_bytes: int) -> ParseResult:
        del max_bytes
        return ParseResult.success(
            {"ok": True, "count": 1},
            raw_output=raw_output,
            strategy=ParseStrategy.NORMALIZED,
        )

    monkeypatch.setattr(service, "_parse_output", unsafe_parse)

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.CONTENT_CHANGED
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["result_json"] is None
    attempts = attempt_rows(repository, job["id"])
    assert len(attempts) == 1
    assert attempts[0]["method"] == "local"
    assert attempts[0]["status"] == "failed"
    assert "CONTENT_CHANGED" in attempts[0]["validation_error"]


# ---------------------------------------------------------------------------
# LLM repair
# ---------------------------------------------------------------------------


async def test_llm_invalid_then_valid_records_attempts_and_usage(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider(["not json", '{"ok": true}'])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=2),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is True
    assert outcome.method is RepairMethod.LLM
    assert outcome.result == {"ok": True}
    assert len(provider.calls) == 2
    for call in provider.calls:
        assert [item["role"] for item in call["messages"]] == ["system", "user"]
        assert "tools" not in call
        assert "history" not in call
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["method"] == "llm"
    attempts = attempt_rows(repository, job["id"])
    assert [(item["method"], item["status"]) for item in attempts] == [
        ("local", "failed"),
        ("llm", "failed"),
        ("llm", "succeeded"),
    ]
    assert attempts[1]["validation_error"]
    assert attempts[2]["validation_error"] is None
    assert attempts[1]["token_usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    assert attempts[1]["request_meta"]["prompt_bytes"] > 0
    assert attempts[1]["request_meta"]["infrastructure_retries"] == 0


async def test_max_llm_attempts_exhausted_fails_closed(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider(["not json", "still not json"])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=2),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.NOT_REPAIRABLE
    assert len(provider.calls) == 2
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "NOT_REPAIRABLE"
    assert stored["result_json"] is None
    attempts = attempt_rows(repository, job["id"])
    assert len(attempts) == 3
    assert attempts[-1]["method"] == "llm"
    assert attempts[-1]["status"] == "failed"


async def test_llm_value_change_is_content_changed(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"status":"bad"}'
    job = make_job(repository, raw, schema=STATUS_SCHEMA)
    claimed = claim(repository)
    provider = FakeProvider(['{"status":"ok"}'])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=2),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.CONTENT_CHANGED
    assert outcome.error is not None
    assert outcome.error.code is ProcessorErrorCode.CONTENT_CHANGED
    assert outcome.finalized is True
    assert len(provider.calls) == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "CONTENT_CHANGED"
    assert stored["result_json"] is None
    attempts = attempt_rows(repository, job["id"])
    assert [item["method"] for item in attempts] == ["local", "llm"]
    assert attempts[-1]["status"] == "failed"


async def test_schema_mismatch_retries_then_fails(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"status":"bad"}'
    job = make_job(repository, raw, schema=STATUS_SCHEMA)
    claimed = claim(repository)
    provider = FakeProvider(['{"status":"bad"}', '{"status":"bad"}'])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=2),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.SCHEMA_VALIDATION_FAILED
    assert len(provider.calls) == 2
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "SCHEMA_VALIDATION_FAILED"
    assert all(
        item["status"] == "failed" for item in attempt_rows(repository, job["id"])
    )


async def test_schema_safe_requires_and_accepts_explicit_allowance(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"name":"x"}'
    settings = llm_settings(repair_mode=RepairMode.SCHEMA_SAFE)

    denied_job = make_job(
        repository,
        raw,
        schema=NOTE_SCHEMA,
        task_run_id="denied-run",
        ref="memory://raw/denied",
    )
    denied_claim = claim(repository)
    denied_provider = FakeProvider(['{"name":"x","note":""}'])
    denied_service = make_service(
        repository, raw=raw, provider=denied_provider, settings=settings
    )
    denied = await denied_service.process_job(denied_claim, "service-owner")

    assert denied.ok is False
    assert denied.error_code is ProcessorErrorCode.CONTENT_CHANGED
    assert repository.get_job(denied_job["id"])["status"] == "failed"

    allowed_job = make_job(
        repository,
        raw,
        schema=NOTE_SCHEMA,
        task_run_id="allowed-run",
        ref="memory://raw/allowed",
    )
    allowed_claim = claim(repository)
    allowed_provider = FakeProvider(['{"name":"x","note":""}'])
    allowed_service = make_service(
        repository, raw=raw, provider=allowed_provider, settings=settings
    )
    allowed = await allowed_service.process_job(
        allowed_claim,
        "service-owner",
        allowances=[placeholder_allowance("$.note")],
    )

    assert allowed.ok is True
    assert allowed.method is RepairMethod.LLM
    assert allowed.result == {"name": "x", "note": ""}
    assert repository.get_job(allowed_job["id"])["status"] == "succeeded"


async def test_infrastructure_retry_does_not_consume_repair_attempt(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider(
        [
            FakeCompletion(
                '{"ok": true}',
                response_format_used=False,
                infrastructure_retries=1,
            )
        ]
    )
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=1),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is True
    assert len(provider.calls) == 1
    attempts = attempt_rows(repository, job["id"])
    llm_attempts = [item for item in attempts if item["method"] == "llm"]
    assert len(llm_attempts) == 1
    assert llm_attempts[0]["request_meta"]["infrastructure_retries"] == 1


# ---------------------------------------------------------------------------
# Provider failures and configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            ProviderError(
                ProviderErrorKind.RATE_LIMITED,
                "provider returned HTTP 429 (rate_limited)",
                status_code=429,
                retryable=True,
            ),
            ProcessorErrorCode.PROVIDER_ERROR,
        ),
        (
            ProviderError(
                ProviderErrorKind.TIMEOUT,
                "provider request timed out",
                retryable=True,
            ),
            ProcessorErrorCode.TIMEOUT,
        ),
        (TimeoutError(), ProcessorErrorCode.TIMEOUT),
    ],
)
async def test_provider_failures_are_typed_and_secret_free(
    repository: JsonProcessorRepository,
    failure: BaseException,
    expected: ProcessorErrorCode,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider([failure])
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(max_llm_attempts=2),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is expected
    assert outcome.finalized is True
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == expected.value
    assert stored["result_json"] is None
    assert raw not in (stored["error_message"] or "")
    attempts = attempt_rows(repository, job["id"])
    assert attempts[-1]["method"] == "llm"
    assert attempts[-1]["status"] == "failed"
    assert attempts[-1]["request_meta"]["error_kind"]


async def test_unconfigured_provider_fails_with_not_configured(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    service = JsonRepairService(
        repository,
        settings=local_only_settings(local_repair_enabled=False, max_llm_attempts=1),
        raw_output_loader=lambda ref: raw,
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.NOT_CONFIGURED
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "NOT_CONFIGURED"


async def test_schema_definition_error_is_typed(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true}'
    job = make_job(
        repository,
        raw,
        task_run_id="bad-schema-run",
        ref="memory://raw/bad-schema",
    )
    # create_or_get_job validates the schema, so replace the stored schema
    # directly to simulate a corrupt/legacy row.
    repository.database._connection.execute(
        "UPDATE json_repair_jobs SET schema_json = ? WHERE id = ?",
        ('{"$ref":"#/definitions/x"}', job["id"]),
    )
    claimed = claim(repository)
    service = make_service(repository, raw=raw, settings=local_only_settings())

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.SCHEMA_DEFINITION_INVALID
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "SCHEMA_DEFINITION_INVALID"


# ---------------------------------------------------------------------------
# Cancellation, lease, and idempotency
# ---------------------------------------------------------------------------


async def test_already_cancelled_job_is_not_processed(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    repository.cancel_job(job["id"], force=True, reason="operator cancelled")
    provider = FakeProvider()
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.CANCELLED
    assert outcome.finalized is True
    assert provider.calls == []
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert attempt_rows(repository, job["id"]) == []


async def test_cancellation_hook_marks_cancelled_before_provider(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider()
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(),
        cancellation_check=lambda received: True,
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.CANCELLED
    assert provider.calls == []
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["error_code"] == "CANCELLED"


async def test_expired_lease_prevents_provider_call(
    database: Database,
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = ? WHERE id = ?",
        (past, job["id"]),
    )
    provider = FakeProvider()
    service = make_service(
        repository,
        raw=raw,
        provider=provider,
        settings=llm_settings(),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert outcome.error_code is ProcessorErrorCode.LEASE_LOST
    assert outcome.finalized is False
    assert provider.calls == []
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "processing"
    assert stored["result_json"] is None


async def test_repeated_processing_is_idempotent(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true,}'
    job = make_job(repository, raw)
    claimed = claim(repository)
    provider = FakeProvider()
    service = make_service(repository, raw=raw, provider=provider)

    first = await service.process_job(claimed, "service-owner")
    second = await service.process_job(claimed, "service-owner")

    assert first.ok is True
    assert second.ok is True
    assert second.finalized is True
    assert second.result == {"ok": True}
    assert repository.list_jobs()["total"] == 1
    assert len(attempt_rows(repository, job["id"])) == 1
    assert provider.calls == []


async def test_worker_counts_service_cancellation_as_cancelled(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru}'
    job = make_job(repository, raw)
    service = make_service(
        repository,
        raw=raw,
        settings=llm_settings(),
        cancellation_check=lambda received: True,
    )
    worker = JsonRepairWorker(
        repository,
        service.process_job,
        owner="cancelled-worker",
        lease_seconds=60,
    )

    stats = await worker.run_once()

    assert stats.claimed == 1
    assert stats.cancelled == 1
    assert stats.failed == 0
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["error_code"] == "CANCELLED"


async def test_worker_callback_does_not_finalize_twice(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": true,}'
    job = make_job(repository, raw)
    service = make_service(repository, raw=raw)
    worker = JsonRepairWorker(
        repository,
        service.process_job,
        owner="service-worker",
        lease_seconds=60,
    )

    stats = await worker.run_once()
    second = await worker.run_once()

    assert stats.claimed == 1
    assert stats.succeeded == 1
    assert stats.failed == 0
    assert stats.conflicts == 0
    assert second.claimed == 0
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["method"] == "local"


# ---------------------------------------------------------------------------
# Raw-output preservation and job-less API
# ---------------------------------------------------------------------------


async def test_raw_output_is_preserved_and_never_in_errors(
    repository: JsonProcessorRepository,
) -> None:
    raw = '{"ok": tru, "marker": "RAW-SECRET-MARKER"}'
    stored_raw: list[tuple[str, str]] = []
    job = make_job(repository, raw)
    claimed = claim(repository)
    service = make_service(
        repository,
        raw=raw,
        settings=local_only_settings(local_repair_enabled=False),
        raw_output_store=lambda ref, value: stored_raw.append((ref, value)),
    )

    outcome = await service.process_job(claimed, "service-owner")

    assert outcome.ok is False
    assert "RAW-SECRET-MARKER" not in (outcome.error_message or "")
    assert stored_raw == [("memory://raw/1", raw)]
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["raw_hash"] == raw_hash(raw)
    assert "RAW-SECRET-MARKER" not in (stored["error_message"] or "")
    assert "RAW-SECRET-MARKER" not in stored["error_code"]


async def test_process_task_output_valid_json_uses_method_none() -> None:
    service = JsonRepairService(settings=local_only_settings())

    result = await service.process_task_output('{"ok": true}', schema=SCHEMA)

    assert result.ok is True
    assert result.value == {"ok": True}
    assert result.method is RepairMethod.NONE
    assert result.attempts == ()


async def test_process_task_output_without_job_returns_typed_result() -> None:
    service = JsonRepairService(settings=local_only_settings())
    raw = '{"ok": true,}'

    result = await service.process_task_output(raw, schema=SCHEMA)

    assert result.ok is True
    assert result.value == {"ok": True}
    assert result.raw_output == raw
    assert result.method is RepairMethod.LOCAL
    assert result.attempts[0].phase.value == "local"


async def test_process_task_output_failure_uses_error_envelope_without_raw() -> None:
    service = JsonRepairService(settings=local_only_settings(local_repair_enabled=False))
    raw = '{"secret":"VALUE-THAT-MUST-NOT-LEAK"'

    result = await service.process_task_output(raw, schema=SCHEMA)

    assert result.ok is False
    assert result.error is not None
    envelope = result.to_envelope()
    assert envelope is not None
    serialized = str(envelope)
    assert "VALUE-THAT-MUST-NOT-LEAK" not in serialized
    assert envelope["valeria_json_processor"]["status"] == "error"


async def test_process_task_output_llm_success_has_no_job_side_effects(
    repository: JsonProcessorRepository,
) -> None:
    provider = FakeProvider(['{"ok": true}'])
    service = JsonRepairService(
        repository,
        provider=provider,
        settings=llm_settings(),
    )

    result = await service.process_task_output('{"ok": tru}', schema=SCHEMA)

    assert result.ok is True
    assert result.value == {"ok": True}
    assert result.method is RepairMethod.LLM
    assert [item.phase.value for item in result.attempts] == ["local", "llm"]
    assert len(provider.calls) == 1
    assert repository.list_jobs()["total"] == 0


def test_processor_error_schema_invalid_alias_is_stable() -> None:
    assert ProcessorErrorCode.SCHEMA_INVALID is ProcessorErrorCode.SCHEMA_VALIDATION_FAILED


def test_processor_error_not_configured_is_typed() -> None:
    error = ProcessorError(ProcessorErrorCode.NOT_CONFIGURED, "provider is disabled")
    assert error.to_envelope()["valeria_json_processor"]["code"] == "NOT_CONFIGURED"
