"""Separate background worker for persisted JSON repair jobs.

This worker is intentionally independent from the OpenCode Task Queue and from
the VFlow runtime.  It only understands the ``json_processor.repository``
contract and an injected processor callback::

    async def processor(job: dict, lease_token: str) -> ProcessorOutcome | Any

The callback is where parsing, local repair, schema validation, and provider
calls live.  The worker never imports a provider and never performs an LLM call
inside a database transaction.  Every repository call is synchronous SQLite and
is moved off the event loop with :func:`asyncio.to_thread`.

Lease behaviour
---------------
``claim_due`` is the only claim path and already implements the CAS/lease
contract.  The worker passes its unique owner token to the processor and to
``complete_job``/``fail_job``, so a worker that lost its lease cannot overwrite a
terminal job.  On :meth:`JsonRepairWorker.stop`, in-flight asyncio tasks are
cancelled and their leases are deliberately abandoned (status remains
``processing``) rather than finalized.  A restart calls
``recover_expired_leases`` on its next pass; the abandoned job becomes claimable
only after its lease expires, which prevents a concurrently running old task
from being processed twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import (
    AttemptPhase,
    ProcessorError,
    ProcessorErrorCode,
    RepairMethod,
    RepairResult,
)
from .repository import (
    JsonProcessorRepository,
    JsonRepairLeaseLost,
    JsonRepairStateConflict,
)

logger = logging.getLogger(__name__)

ProcessorCallable = Callable[[dict[str, Any], str], Any]


@dataclass(frozen=True, slots=True)
class ProcessorOutcome:
    """Result returned by an injected processor callback.

    A callback may instead return a raw JSON-serializable value (treated as a
    successful result with ``method='none'``) or a core ``RepairResult``.
    Failures can be expressed either by raising a typed exception (for example
    ``ProcessorError``) or by returning ``ProcessorOutcome.failure(...)``.

    ``finalized=True`` means the callback already completed/failed the job
    through the repository CAS (for example the M5 orchestration service); the
    worker must then only account for the outcome and must not finalize twice.
    """

    result: Any = None
    method: RepairMethod = RepairMethod.NONE
    ok: bool = True
    error_code: ProcessorErrorCode | str | None = None
    error_message: str = ""
    finalized: bool = False

    @classmethod
    def success(
        cls,
        result: Any,
        *,
        method: RepairMethod | str = RepairMethod.NONE,
        finalized: bool = False,
    ) -> ProcessorOutcome:
        return cls(
            result=result,
            method=_coerce_method(method),
            finalized=finalized,
        )

    @classmethod
    def failure(
        cls,
        error_code: ProcessorErrorCode | str,
        error_message: str = "",
        *,
        method: RepairMethod | str = RepairMethod.NONE,
        finalized: bool = False,
    ) -> ProcessorOutcome:
        return cls(
            method=_coerce_method(method),
            ok=False,
            error_code=error_code,
            error_message=error_message,
            finalized=finalized,
        )

    @property
    def error(self) -> ProcessorError | None:
        """Typed error for a failed outcome, or ``None`` on success."""

        if self.ok:
            return None
        try:
            code = _coerce_error_code(self.error_code)
        except ValueError:
            code = ProcessorErrorCode.INTERNAL_ERROR
        return ProcessorError(code, self.error_message or "repair failed")


@dataclass(frozen=True, slots=True)
class WorkerRunStats:
    """One ``run_once`` pass summary."""

    recovered: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    conflicts: int = 0
    cancelled: int = 0

    @property
    def processed(self) -> int:
        return self.succeeded + self.failed

    def to_dict(self) -> dict[str, int]:
        return {
            "recovered": self.recovered,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "conflicts": self.conflicts,
            "cancelled": self.cancelled,
            "processed": self.processed,
        }


class QueueLimitExceeded(RuntimeError):
    """The queued-job admission limit would be exceeded."""

    def __init__(self, queue_depth: int, queue_limit: int) -> None:
        self.queue_depth = queue_depth
        self.queue_limit = queue_limit
        super().__init__(
            f"json repair queue is full ({queue_depth}/{queue_limit} queued)"
        )


class _JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"


def _positive_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, value: Any, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not 0 < result <= maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return result


def _coerce_method(value: RepairMethod | str) -> RepairMethod:
    if isinstance(value, RepairMethod):
        return value
    if isinstance(value, str):
        return RepairMethod(value)
    raise ValueError("method must be a RepairMethod or string")


def _coerce_error_code(value: ProcessorErrorCode | str | None) -> ProcessorErrorCode:
    if isinstance(value, ProcessorErrorCode):
        return value
    if isinstance(value, str):
        return ProcessorErrorCode(value)
    return ProcessorErrorCode.INTERNAL_ERROR


def _safe_error_text(value: Any, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if not text:
        return fallback
    return text[:2_000]


def _classify_exception(exc: BaseException) -> tuple[ProcessorErrorCode, str]:
    if isinstance(exc, ProcessorError):
        return exc.code, _safe_error_text(exc.message, fallback="processor error")
    processor_code = getattr(exc, "processor_code", None)
    if processor_code is None:
        raw_code = getattr(exc, "code", None)
        if isinstance(raw_code, ProcessorErrorCode):
            processor_code = raw_code
    if isinstance(processor_code, ProcessorErrorCode):
        return processor_code, _safe_error_text(str(exc), fallback="processor error")
    if isinstance(processor_code, str):
        try:
            return ProcessorErrorCode(processor_code), _safe_error_text(
                str(exc), fallback="processor error"
            )
        except ValueError:
            pass
    if isinstance(exc, TimeoutError):
        return ProcessorErrorCode.TIMEOUT, "processor timed out"
    # Do not copy arbitrary exception text into persistent job metadata; it may
    # contain raw model output or credentials from an unrelated library.
    return (
        ProcessorErrorCode.INTERNAL_ERROR,
        f"processor raised {type(exc).__name__}",
    )


def _method_from_repair_result(result: RepairResult) -> RepairMethod:
    if any(attempt.phase is AttemptPhase.LLM for attempt in result.attempts):
        return RepairMethod.LLM
    if result.attempts:
        return RepairMethod.LOCAL
    return RepairMethod.NONE


def _normalize_outcome(value: Any) -> ProcessorOutcome:
    if isinstance(value, ProcessorOutcome):
        method = _coerce_method(value.method)
        finalized = bool(getattr(value, "finalized", False))
        if value.ok:
            return ProcessorOutcome(
                result=value.result,
                method=method,
                finalized=finalized,
            )
        return ProcessorOutcome(
            method=method,
            ok=False,
            error_code=_coerce_error_code(value.error_code),
            error_message=_safe_error_text(
                value.error_message, fallback="processor reported failure"
            ),
            finalized=finalized,
        )
    if isinstance(value, RepairResult):
        method = _method_from_repair_result(value)
        if value.ok:
            return ProcessorOutcome(result=value.value, method=method)
        error = value.error
        if error is None:
            return ProcessorOutcome(
                method=method,
                ok=False,
                error_code=ProcessorErrorCode.INTERNAL_ERROR,
                error_message="repair pipeline failed without an error",
            )
        return ProcessorOutcome(
            method=method,
            ok=False,
            error_code=error.code,
            error_message=_safe_error_text(error.message, fallback="repair failed"),
        )
    if isinstance(value, BaseException):
        raise TypeError("processor returned an exception instead of a result")
    return ProcessorOutcome(result=value)


async def enqueue(
    repository: JsonProcessorRepository,
    *,
    queue_limit: int,
    **job_kwargs: Any,
) -> dict[str, Any]:
    """Create a repair job after a best-effort queued-depth admission check.

    This helper does not introduce a service or a second queue.  The canonical
    idempotency rules still live in the repository.  The depth check and insert
    are not atomic; a caller that needs a hard limit must serialize admissions or
    enforce the limit at its own API boundary.
    """

    limit = _positive_int("queue_limit", queue_limit, minimum=1, maximum=100_000)
    depth = await asyncio.to_thread(repository.queue_depth)
    queued = int(depth.get("queued", 0))
    if queued >= limit:
        raise QueueLimitExceeded(queued, limit)
    return await asyncio.to_thread(repository.create_or_get_job, **job_kwargs)


class JsonRepairWorker:
    """One worker instance with a bounded number of concurrent jobs.

    ``concurrency_limit`` is the maximum number of in-flight processor callbacks
    for this worker (and therefore the maximum number of jobs claimed in one
    ``run_once`` pass).  Multiple workers may share a repository; the repository
    CAS prevents duplicate claims.
    """

    def __init__(
        self,
        repository: JsonProcessorRepository,
        processor: ProcessorCallable,
        concurrency_limit: int = 4,
        *,
        lease_seconds: int = 120,
        poll_interval: float = 1.0,
        recover_limit: int = 500,
        owner: str | None = None,
        queue_limit: int | None = None,
    ) -> None:
        if not callable(processor):
            raise TypeError("processor must be callable")
        self._repository = repository
        self._processor = processor
        self._concurrency_limit = _positive_int(
            "concurrency_limit", concurrency_limit, minimum=1, maximum=32
        )
        self._lease_seconds = _positive_int(
            "lease_seconds", lease_seconds, minimum=1, maximum=86_400
        )
        self._poll_interval = _positive_float(
            "poll_interval", poll_interval, maximum=300.0
        )
        self._recover_limit = _positive_int(
            "recover_limit", recover_limit, minimum=1, maximum=10_000
        )
        if owner is None:
            owner = f"json-repair-worker-{uuid.uuid4().hex}"
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")
        owner = owner.strip()
        if len(owner) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in owner):
            raise ValueError("owner must be at most 200 characters without controls")
        self._owner = owner
        self._queue_limit = (
            None
            if queue_limit is None
            else _positive_int("queue_limit", queue_limit, minimum=1, maximum=100_000)
        )

        self._inflight: set[asyncio.Task[_JobOutcome]] = set()
        self._claim_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stopping = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def owner(self) -> str:
        return self._owner

    @property
    def concurrency_limit(self) -> int:
        return self._concurrency_limit

    @property
    def is_running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def inflight_count(self) -> int:
        return sum(1 for task in self._inflight if not task.done())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the background loop (idempotent)."""

        if self.is_running:
            return
        self._stopping = False
        # Recreate the event so a worker can be restarted on another loop in
        # tests without inheriting a wait from its previous loop.
        self._stop_event = asyncio.Event()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name=f"json-repair-worker:{self._owner}",
        )

    async def stop(self) -> None:
        """Cancel in-flight work and stop the loop.

        Leases are abandoned, not failed or completed: the jobs remain
        ``processing`` until their lease expires, at which point
        ``recover_expired_leases`` makes them claimable again.  This avoids
        rewriting a terminal job and avoids running the same job twice while a
        cancelled processor thread is still winding down.
        """

        self._stopping = True
        self._stop_event.set()
        runner = self._runner
        self._runner = None
        current = asyncio.current_task()
        if runner is not None and runner is not current and current not in self._inflight:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

        tasks = [
            task
            for task in list(self._inflight)
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._inflight.discard(task)

    async def run_once(self) -> WorkerRunStats:
        """Recover expired leases and process at most one concurrency batch."""

        recovered = await asyncio.to_thread(
            self._repository.recover_expired_leases,
            limit=self._recover_limit,
        )
        async with self._claim_lock:
            active = self.inflight_count
            available = self._concurrency_limit - active
            if available <= 0:
                return WorkerRunStats(recovered=recovered)
            jobs = await asyncio.to_thread(
                self._repository.claim_due,
                available,
                self._lease_seconds,
                owner=self._owner,
            )
            tasks: list[asyncio.Task[_JobOutcome]] = []
            for job in jobs:
                task = asyncio.create_task(
                    self._process_job(job),
                    name=f"json-repair-job:{job.get('id')}",
                )
                self._inflight.add(task)
                task.add_done_callback(self._discard_task)
                tasks.append(task)

        if not tasks:
            return WorkerRunStats(recovered=recovered)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        succeeded = failed = conflicts = cancelled = 0
        for task, result in zip(tasks, results, strict=True):
            if result is _JobOutcome.SUCCEEDED:
                succeeded += 1
            elif result is _JobOutcome.FAILED:
                failed += 1
            elif result is _JobOutcome.CONFLICT:
                conflicts += 1
            elif result is _JobOutcome.CANCELLED or isinstance(
                result, asyncio.CancelledError
            ):
                cancelled += 1
            elif isinstance(result, BaseException):
                # _process_job normally converts exceptions into a failed job.
                # A BaseException that escapes is still counted so a run pass
                # never silently loses work.
                logger.error(
                    "json repair worker task %s failed unexpectedly",
                    task.get_name(),
                )
                failed += 1
        return WorkerRunStats(
            recovered=recovered,
            claimed=len(tasks),
            succeeded=succeeded,
            failed=failed,
            conflicts=conflicts,
            cancelled=cancelled,
        )

    async def enqueue(self, **job_kwargs: Any) -> dict[str, Any]:
        """Create a job through the worker's optional admission limit."""

        if self._queue_limit is None:
            return await asyncio.to_thread(
                self._repository.create_or_get_job, **job_kwargs
            )
        return await enqueue(
            self._repository,
            queue_limit=self._queue_limit,
            **job_kwargs,
        )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------
    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("json repair worker iteration failed")
            if self._stopping:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )

    def _discard_task(self, task: asyncio.Task[_JobOutcome]) -> None:
        self._inflight.discard(task)

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------
    async def _invoke_processor(
        self,
        job: dict[str, Any],
        lease_token: str,
    ) -> Any:
        if inspect.iscoroutinefunction(self._processor):
            return await self._processor(job, lease_token)
        result = await asyncio.to_thread(self._processor, job, lease_token)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _process_job(self, job: Mapping[str, Any]) -> _JobOutcome:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            logger.error("json repair worker received a job without an id")
            return _JobOutcome.FAILED
        try:
            raw_outcome = await self._invoke_processor(dict(job), self._owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._fail_from_exception(job_id, exc)

        try:
            outcome = _normalize_outcome(raw_outcome)
        except Exception:
            return await self._record_failure(
                job_id,
                ProcessorErrorCode.INTERNAL_ERROR,
                "processor returned an invalid result",
            )

        if outcome.finalized:
            # The callback already used the repository CAS to finalize this
            # job.  Do not rewrite it a second time.
            if outcome.ok:
                return _JobOutcome.SUCCEEDED
            if outcome.error_code is ProcessorErrorCode.CANCELLED:
                return _JobOutcome.CANCELLED
            return _JobOutcome.FAILED

        if not outcome.ok:
            return await self._record_failure(
                job_id,
                outcome.error_code,
                outcome.error_message,
                method=outcome.method,
            )

        try:
            await asyncio.to_thread(
                self._repository.complete_job,
                job_id,
                lease_owner=self._owner,
                result_json=outcome.result,
                method=outcome.method,
            )
        except (JsonRepairStateConflict, JsonRepairLeaseLost):
            # A terminal job or a lease lost to another worker must not be
            # rewritten.  The repository guards are the source of truth.
            return _JobOutcome.CONFLICT
        except Exception as exc:
            return await self._fail_from_exception(job_id, exc)
        return _JobOutcome.SUCCEEDED

    async def _fail_from_exception(
        self,
        job_id: str,
        exc: BaseException,
    ) -> _JobOutcome:
        code, message = _classify_exception(exc)
        return await self._record_failure(job_id, code, message)

    async def _record_failure(
        self,
        job_id: str,
        error_code: ProcessorErrorCode | str | None,
        error_message: str,
        *,
        method: RepairMethod | str | None = None,
    ) -> _JobOutcome:
        try:
            code = _coerce_error_code(error_code)
        except ValueError:
            code = ProcessorErrorCode.INTERNAL_ERROR
        safe_method: RepairMethod | None
        try:
            safe_method = None if method is None else _coerce_method(method)
        except ValueError:
            safe_method = None
        try:
            await asyncio.to_thread(
                self._repository.fail_job,
                job_id,
                lease_owner=self._owner,
                error_code=code,
                error_message=_safe_error_text(
                    error_message, fallback="processor failed"
                ),
                method=safe_method,
            )
        except (JsonRepairStateConflict, JsonRepairLeaseLost):
            return _JobOutcome.CONFLICT
        except Exception:
            logger.exception(
                "json repair worker could not record failure for job %s", job_id
            )
            return _JobOutcome.CONFLICT
        return _JobOutcome.FAILED


__all__ = [
    "JsonRepairWorker",
    "ProcessorOutcome",
    "QueueLimitExceeded",
    "WorkerRunStats",
    "enqueue",
]
