from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencode_web.database import Database
from opencode_web.json_processor import (
    ProcessorError,
    ProcessorErrorCode,
    RepairJobStatus,
    RepairMethod,
    RepairSourceType,
)
from opencode_web.json_processor.repository import JsonProcessorRepository
from opencode_web.json_processor.worker import (
    JsonRepairWorker,
    ProcessorOutcome,
    QueueLimitExceeded,
    enqueue,
)

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "json-processor-worker.sqlite3", "s" * 32)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def repository(database: Database) -> JsonProcessorRepository:
    return JsonProcessorRepository(database)


def make_job(
    repository: JsonProcessorRepository,
    *,
    index: int = 1,
    task_run_id: str = "task-run-worker",
    raw: str | None = None,
) -> dict:
    raw_text = raw if raw is not None else f"raw-{index}"
    return repository.create_or_get_job(
        source_type=RepairSourceType.TASK,
        task_run_id=task_run_id,
        raw_output_ref=f"artifact://raw/{index}",
        raw_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        schema=SCHEMA,
        prompt_version="v1",
    )


def job_kwargs(
    *,
    index: int = 2,
    raw: str | None = None,
    task_run_id: str = "task-run-worker",
) -> dict:
    raw_text = raw if raw is not None else f"raw-{index}"
    return {
        "source_type": RepairSourceType.TASK,
        "task_run_id": task_run_id,
        "raw_output_ref": f"artifact://raw/{index}",
        "raw_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "schema": SCHEMA,
        "prompt_version": "v1",
    }


async def test_worker_claims_completes_and_passes_lease_token(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    calls: list[tuple[str, str]] = []

    async def processor(received: dict, lease_token: str) -> ProcessorOutcome:
        calls.append((received["id"], lease_token))
        return ProcessorOutcome.success({"ok": True}, method=RepairMethod.LOCAL)

    worker = JsonRepairWorker(
        repository,
        processor,
        concurrency_limit=2,
        lease_seconds=60,
        owner="worker-a",
    )

    stats = await worker.run_once()

    assert stats.recovered == 0
    assert stats.claimed == 1
    assert stats.succeeded == 1
    assert stats.failed == 0
    assert stats.conflicts == 0
    assert stats.processed == 1
    assert calls == [(job["id"], "worker-a")]
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == RepairJobStatus.SUCCEEDED.value
    assert stored["result"] == {"ok": True}
    assert stored["method"] == RepairMethod.LOCAL.value
    assert stored["attempts"] == 1
    assert stored["lease_owner"] is None
    assert stored["lease_until"] is None
    assert stored["finished_at"] is not None
    assert worker.inflight_count == 0


async def test_sync_processor_is_supported(repository: JsonProcessorRepository) -> None:
    job = make_job(repository)

    def processor(received: dict, lease_token: str) -> dict:
        assert received["id"] == job["id"]
        assert lease_token == "sync-worker"
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="sync-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.succeeded == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["result"] == {"ok": True}
    assert stored["method"] == "none"


async def test_concurrency_limit_is_respected(repository: JsonProcessorRepository) -> None:
    jobs = [make_job(repository, index=index, raw=f"raw-{index}") for index in range(5)]
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []
    active = 0
    max_active = 0

    async def processor(job: dict, lease_token: str) -> dict:
        nonlocal active, max_active
        del lease_token
        active += 1
        max_active = max(max_active, active)
        seen.append(job["id"])
        if len(seen) >= 2:
            started.set()
        await release.wait()
        active -= 1
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        concurrency_limit=2,
        lease_seconds=60,
        owner="concurrency-worker",
    )
    run_task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=2)
    assert max_active == 2
    assert len(seen) == 2
    assert repository.queue_depth()["queued"] == 3

    release.set()
    stats = await asyncio.wait_for(run_task, timeout=2)

    assert stats.claimed == 2
    assert stats.succeeded == 2
    assert stats.failed == 0
    assert max_active == 2
    assert repository.queue_depth()["queued"] == 3
    assert repository.queue_depth()["succeeded"] == 2
    assert {job["id"] for job in jobs}.issuperset(seen)


async def test_expired_lease_is_recovered_then_processed(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="stale-worker")
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = ? WHERE id = ?",
        (past, job["id"]),
    )

    async def processor(received: dict, lease_token: str) -> dict:
        assert received["id"] == job["id"]
        assert lease_token == "recovery-worker"
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="recovery-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.recovered == 1
    assert stats.claimed == 1
    assert stats.succeeded == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["attempts"] == 2
    assert stored["lease_owner"] is None


async def test_processing_job_without_lease_is_recovered(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="stale-worker")
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = NULL WHERE id = ?",
        (job["id"],),
    )

    async def processor(received: dict, lease_token: str) -> dict:
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="recovery-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.recovered == 1
    assert stats.succeeded == 1
    assert repository.get_job(job["id"])["status"] == "succeeded"


async def test_stop_cancels_inflight_and_restart_does_not_duplicate(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    first_calls = 0

    async def hanging_processor(received: dict, lease_token: str) -> dict:
        nonlocal first_calls
        del received, lease_token
        first_calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    worker = JsonRepairWorker(
        repository,
        hanging_processor,
        concurrency_limit=2,
        lease_seconds=60,
        owner="worker-a",
        poll_interval=0.01,
    )
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await worker.stop()

    assert cancelled.is_set()
    assert first_calls == 1
    abandoned = repository.get_job(job["id"])
    assert abandoned is not None
    assert abandoned["status"] == "processing"
    assert abandoned["lease_owner"] == "worker-a"
    assert abandoned["finished_at"] is None

    restart_calls = 0

    async def restart_processor(received: dict, lease_token: str) -> dict:
        nonlocal restart_calls
        restart_calls += 1
        return {"ok": True}

    restarted = JsonRepairWorker(
        repository,
        restart_processor,
        owner="worker-b",
        lease_seconds=60,
    )
    # The abandoned lease is still active, so restarting must not process the
    # job twice before expiry.
    early = await restarted.run_once()
    assert early.recovered == 0
    assert early.claimed == 0
    assert restart_calls == 0
    assert repository.get_job(job["id"])["status"] == "processing"

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = ? WHERE id = ?",
        (past, job["id"]),
    )
    stats = await restarted.run_once()

    assert stats.recovered == 1
    assert stats.claimed == 1
    assert stats.succeeded == 1
    assert restart_calls == 1
    assert first_calls == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["attempts"] == 2


async def test_processor_provider_error_fails_job_with_typed_code(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)

    async def processor(received: dict, lease_token: str) -> dict:
        raise ProcessorError(ProcessorErrorCode.PROVIDER_ERROR, "provider failed")

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="failing-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.claimed == 1
    assert stats.failed == 1
    assert stats.succeeded == 0
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == ProcessorErrorCode.PROVIDER_ERROR.value
    assert stored["error_message"] == "provider failed"
    assert stored["result_json"] is None
    assert stored["lease_owner"] is None


async def test_processor_outcome_failure_fails_job(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)

    async def processor(received: dict, lease_token: str) -> ProcessorOutcome:
        return ProcessorOutcome.failure(
            ProcessorErrorCode.TIMEOUT,
            "model timed out",
            method=RepairMethod.LLM,
        )

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="outcome-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.failed == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "TIMEOUT"
    assert stored["error_message"] == "model timed out"
    assert stored["method"] == "llm"


async def test_unexpected_processor_exception_does_not_leak_raw_message(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)

    async def processor(received: dict, lease_token: str) -> dict:
        raise RuntimeError("raw output secret should not be persisted")

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="unexpected-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.failed == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "INTERNAL_ERROR"
    assert "processor raised RuntimeError" in stored["error_message"]
    assert "raw output secret" not in stored["error_message"]


async def test_processor_exception_with_processor_code_is_preserved(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)

    class FakeProviderError(Exception):
        processor_code = ProcessorErrorCode.LLM_UNAVAILABLE

    async def processor(received: dict, lease_token: str) -> dict:
        raise FakeProviderError("provider offline")

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="provider-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.failed == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["error_code"] == "LLM_UNAVAILABLE"
    assert stored["error_message"] == "provider offline"


async def test_terminal_job_is_not_rewritten_by_completion(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)

    async def processor(received: dict, lease_token: str) -> dict:
        await asyncio.to_thread(
            repository.cancel_job,
            received["id"],
            force=True,
            reason="operator cancelled",
        )
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="terminal-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.claimed == 1
    assert stats.succeeded == 0
    assert stats.conflicts == 1
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["error_message"] == "operator cancelled"
    assert stored["result_json"] is None
    assert stored["finished_at"] is not None


async def test_lease_lost_during_processing_is_not_rewritten(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    saved_lease_expiry: list[str] = []

    async def processor(received: dict, lease_token: str) -> dict:
        stored = repository.get_job(received["id"])
        assert stored is not None
        saved_lease_expiry.append(stored["lease_until"])
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await asyncio.to_thread(
            lambda: database._connection.execute(
                "UPDATE json_repair_jobs SET lease_until = ? WHERE id = ?",
                (past, received["id"]),
            )
        )
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="lease-worker",
        lease_seconds=60,
    )
    stats = await worker.run_once()

    assert stats.conflicts == 1
    assert stats.succeeded == 0
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "processing"
    assert stored["result_json"] is None
    assert repository.recover_expired_leases() == 1


async def test_two_workers_do_not_process_the_same_job(
    repository: JsonProcessorRepository,
) -> None:
    jobs = [make_job(repository, index=index, raw=f"raw-{index}") for index in range(4)]
    calls: list[str] = []

    async def processor(job: dict, lease_token: str) -> dict:
        calls.append(job["id"])
        await asyncio.sleep(0.01)
        return {"ok": True}

    worker_a = JsonRepairWorker(
        repository,
        processor,
        concurrency_limit=2,
        lease_seconds=60,
        owner="worker-a",
    )
    worker_b = JsonRepairWorker(
        repository,
        processor,
        concurrency_limit=2,
        lease_seconds=60,
        owner="worker-b",
    )
    stats_a, stats_b = await asyncio.gather(
        worker_a.run_once(),
        worker_b.run_once(),
    )

    assert stats_a.claimed + stats_b.claimed == 4
    assert stats_a.succeeded + stats_b.succeeded == 4
    assert sorted(calls) == sorted(job["id"] for job in jobs)
    assert len(calls) == len(set(calls)) == 4
    for job in jobs:
        stored = repository.get_job(job["id"])
        assert stored is not None
        assert stored["attempts"] == 1
        assert stored["status"] == "succeeded"


async def test_enqueue_honors_queue_limit(repository: JsonProcessorRepository) -> None:
    make_job(repository, index=1, raw="existing")

    with pytest.raises(QueueLimitExceeded) as info:
        await enqueue(repository, queue_limit=1, **job_kwargs(index=2))
    assert info.value.queue_depth == 1
    assert info.value.queue_limit == 1
    assert repository.list_jobs()["total"] == 1

    repository.claim_due(1, lease_seconds=60, owner="admission-worker")
    created = await enqueue(repository, queue_limit=1, **job_kwargs(index=2))
    assert created["status"] == "queued"
    assert repository.list_jobs()["total"] == 2


async def test_worker_enqueue_uses_configured_queue_limit(
    repository: JsonProcessorRepository,
) -> None:
    make_job(repository, index=1, raw="existing")

    async def processor(received: dict, lease_token: str) -> dict:
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="admission-worker",
        queue_limit=1,
    )
    with pytest.raises(QueueLimitExceeded):
        await worker.enqueue(**job_kwargs(index=2))

    repository.claim_due(1, lease_seconds=60, owner="admission-worker")
    created = await worker.enqueue(**job_kwargs(index=2))
    assert created["status"] == "queued"


async def test_start_is_idempotent_and_stop_is_safe(repository: JsonProcessorRepository) -> None:
    job = make_job(repository)
    calls = 0

    async def processor(received: dict, lease_token: str) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": True}

    worker = JsonRepairWorker(
        repository,
        processor,
        owner="lifecycle-worker",
        lease_seconds=60,
        poll_interval=0.01,
    )
    await worker.start()
    await worker.start()
    deadline = asyncio.get_running_loop().time() + 2
    while calls == 0 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await worker.stop()
    await worker.stop()
    await asyncio.sleep(0.03)

    assert calls == 1
    assert not worker.is_running
    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
