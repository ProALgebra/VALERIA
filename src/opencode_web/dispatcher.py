from __future__ import annotations

import asyncio
import logging
import re
from asyncio import Lock
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any
from weakref import WeakValueDictionary

from .consultations import CONSULTATION_PERMISSIONS, CONSULTATION_SYSTEM, final_answer
from .database import Database
from .events import EventBroker
from .items import publish_chat_updated
from .opencode_client import OpenCodeError, OpenCodeManager, OpenCodeReadTimeout
from .questions import question_event, session_questions
from .task_results import task_final_answer

logger = logging.getLogger(__name__)
STOP_RETRY_SECONDS = 5
POLL_READ_RETRY_INITIAL_SECONDS = 1
POLL_READ_RETRY_MAX_SECONDS = 30
CONTINUE_PROMPT = "План принят. Продолжай: реализуй предложенный план."
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class TaskExecutionError(RuntimeError):
    """A task cannot continue without interactive input or a valid final response."""


class QueueDispatcher:
    def __init__(
        self,
        database: Database,
        manager: OpenCodeManager,
        events: EventBroker,
        default_limit: int,
        per_user_default: int | None = None,
    ):
        self.database = database
        self.manager = manager
        self.events = events
        self.default_limit = default_limit
        self.per_user_default = per_user_default
        self._task: asyncio.Task[None] | None = None
        self._workers: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._stopping = False
        self._job_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()
        self._cancellations: dict[str, asyncio.Task[None]] = {}

    async def _poll_opencode(
        self,
        job_id: str,
        operation: str,
        request: Callable[[], Awaitable[Any]],
    ) -> Any:
        delay = POLL_READ_RETRY_INITIAL_SECONDS
        while True:
            try:
                return await request()
            except OpenCodeReadTimeout:
                if self._stopping:
                    raise asyncio.CancelledError from None
                logger.warning(
                    "OpenCode poll timed out; retrying in %s seconds",
                    delay,
                    extra={"job_id": job_id, "operation": operation},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, POLL_READ_RETRY_MAX_SECONDS)

    def _job_lock(self, job_id: str) -> Lock:
        lock = self._job_locks.get(job_id)
        if lock is None:
            lock = Lock()
            self._job_locks[job_id] = lock
        return lock

    async def stop_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.database.get_job(job_id)
        if not job:
            return None
        if job["status"] in TERMINAL_STATUSES:
            return job
        before = job["status"]
        if not self.database.cancel_queued_job(job_id):
            self.database.set_job_status(job_id, "cancelling")
        job = self.database.get_job(job_id)
        if job["status"] == "cancelling":
            self._start_cancellation(job_id)
        await self._publish_job_update(job_id, finished_from=before)
        self.wake()
        return job

    def _start_cancellation(self, job_id: str) -> None:
        existing = self._cancellations.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._cancel_job(job_id), name=f"cancel-job-{job_id}")
        self._cancellations[job_id] = task
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        def forget(done: asyncio.Task[None]) -> None:
            if self._cancellations.get(job_id) is done:
                self._cancellations.pop(job_id, None)

        task.add_done_callback(forget)

    async def _cancel_job(self, job_id: str) -> None:
        while not self._stopping:
            # Serialize abort with session creation/prompt submission, so a late
            # prompt cannot start after we confirmed cancellation and freed its slot.
            async with self._job_lock(job_id):
                job = self.database.get_job(job_id)
                if not job or job["status"] != "cancelling":
                    return
                try:
                    if not job.get("session_pending"):
                        client = await self.manager.ensure(job["workspace_snapshot"])
                        if not await client.abort(job["session_id"]):
                            raise OpenCodeError("Cancellation not accepted")
                        statuses = await client.session_statuses()
                        if self._busy(statuses.get(job["session_id"])):
                            raise OpenCodeError("Session still running")
                except Exception:
                    # Keep capacity reserved on an uncertain outcome. Retry also
                    # survives restart through the durable cancelling status.
                    first_failure = job.get("error_code") != "stop_unconfirmed"
                    self.database.set_job_status(job_id, "cancelling", "stop_unconfirmed")
                    if first_failure:
                        self.database.audit(
                            None, "job.stop.retry", effective_user_id=job["effective_user_id"],
                            object_type="job", object_id=job_id,
                        )
                        await self._publish_job_update(job_id)
                else:
                    self.database.set_job_status(job_id, "cancelled")
                    self.database.audit(
                        None, "job.stop.completed", effective_user_id=job["effective_user_id"],
                        object_type="job", object_id=job_id,
                    )
                    await self._publish_job_update(job_id, finished_from="cancelling")
                    self.wake()
                    return
            await asyncio.sleep(STOP_RETRY_SECONDS)

    @property
    def limit(self) -> int:
        return self.database.concurrency_override() or self.default_limit

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="queue-dispatcher")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._workers:
            for worker in tuple(self._workers):
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)

    async def _run(self) -> None:
        await self.recover()
        while not self._stopping:
            launched = False
            while job := self.database.claim_next_job(self.limit, self.per_user_default):
                launched = True
                await self._publish_job_update(job["id"])
                task = asyncio.create_task(self._execute(job), name=f"job-{job['id']}")
                self._workers.add(task)
                task.add_done_callback(self._workers.discard)
            if not launched:
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=2)

    def _spawn(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def recover(self) -> None:
        """Reconcile jobs which were active when the gateway stopped.

        A dispatching job without a persisted OpenCode message is safe to put back
        into FIFO. Once OpenCode accepted the message, an interrupted generation is
        never resent automatically: doing so could duplicate filesystem changes.
        """
        for job in self.database.active_jobs():
            job = self.database.get_job(job["id"])
            if job["status"] == "cancelling":
                self._start_cancellation(job["id"])
                continue
            # Set to None once a worker takes the job over and reports its end itself.
            before: str | None = job["status"]
            try:
                if job.get("session_pending"):
                    self.database.requeue_interrupted_job(job["id"])
                    continue
                client = await self.manager.ensure(job["workspace_snapshot"])
                statuses = await self._poll_opencode(
                    job["id"], "recovery-session-statuses", client.session_statuses
                )
                busy = self._busy(statuses.get(job["session_id"]))
                if job.get("consultation"):
                    pending = await self._poll_opencode(
                        job["id"],
                        "recovery-consultation-questions",
                        partial(
                            session_questions,
                            client,
                            job["session_id"],
                            job["workspace_snapshot"],
                        ),
                    )
                    if pending:
                        cleared = self.database.set_job_status(job["id"], "waiting_question")
                        await self._publish_job_update(job["id"])
                        if cleared:
                            await self._publish_chat_update(job["chat_id"])
                        continue
                    if job["status"] == "waiting_question":
                        # The reply may have reached OpenCode just before a gateway
                        # restart. Resume monitoring through FIFO, never the prompt.
                        self.database.queue_consultation_resume(job["id"])
                        await self._publish_job_update(job["id"])
                        self.wake()
                        continue
                task_pending = False
                if (job.get("task_id") or job.get("consultation")) and not busy and (
                    job["status"] != "cancelling"
                ):
                    questions = await self._poll_opencode(
                        job["id"], "recovery-questions", client.questions
                    )
                    permissions = await self._poll_opencode(
                        job["id"], "recovery-permissions", client.permissions
                    )
                    task_pending = any(
                        request.get("sessionID") == job["session_id"]
                        for request in [*questions, *permissions]
                    )
                if not job.get("task_id") and not job.get("consultation") and not busy and (
                    job["status"] != "cancelling"
                ):
                    task_pending = bool(
                        await self._poll_opencode(
                            job["id"],
                            "recovery-chat-questions",
                            partial(
                                session_questions,
                                client,
                                job["session_id"],
                                job["workspace_snapshot"],
                            ),
                        )
                    )
                if busy or task_pending:
                    self.database.set_job_status(job["id"], "running")
                    before = None
                    self._spawn(self._execute(job, client), f"recovered-job-{job['id']}")
                else:
                    messages = await client.messages(job["session_id"])
                    user_found, result = self._message_result(
                        messages, job.get("phase2_message_id") or job["message_id"],
                        task_session=bool(job.get("task_id"))
                    )
                    if job.get("consultation") and final_answer(messages, job["message_id"]):
                        result = "completed"
                    if job.get("phase2_message_id") and not user_found:
                        # A crash may happen before or after POST acceptance. An
                        # absent message is not proof that retrying is safe.
                        self.database.set_job_status(
                            job["id"], "failed", "build_submission_uncertain"
                        )
                    elif (result == "completed" and job.get("plan_first")
                          and not job.get("phase2_message_id")):
                        self.database.set_job_status(job["id"], "running")
                        before = None
                        self._spawn(self._execute(job, client), f"recovered-job-{job['id']}")
                    elif job["status"] == "dispatching" and not user_found:
                        self.database.requeue_interrupted_job(job["id"])
                    elif result == "completed":
                        if job.get("consultation"):
                            answer = final_answer(messages, job["message_id"])
                            if answer:
                                self.database.complete_consultation_run(job["id"], answer)
                            else:
                                self.database.set_job_status(
                                    job["id"], "failed", "incomplete_response"
                                )
                                await self._publish_job_update(job["id"])
                                continue
                        elif job.get("task_id"):
                            self.database.complete_task_run(job["id"], task_final_answer(
                                messages, job.get("phase2_message_id") or job["message_id"]
                            ))
                        else:
                            self.database.set_job_status(job["id"], "completed")
                    elif result == "failed":
                        self.database.set_job_status(job["id"], "failed", "assistant_error")
                    else:
                        self.database.set_job_status(job["id"], "failed", "interrupted_by_restart")
            except OpenCodeError:
                logger.exception("OpenCode recovery failed", extra={"job_id": job["id"]})
                self.database.set_job_status(job["id"], "failed", "recovery_unavailable")
            await self._publish_job_update(job["id"], finished_from=before)

    async def _publish_chat_update(self, chat_id: str) -> None:
        await publish_chat_updated(self.database, self.events, chat_id)

    async def _publish_job_update(
        self, job_id: str, *, finished_from: str | None = None
    ) -> None:
        """Publish job.updated. Pass the status seen before a transition this caller
        made: when the job has just ended, a folder chat also gets chat.updated."""
        job = self.database.get_job(job_id)
        await self.events.publish({"type": "job.updated", "job": job})
        if job and job["status"] == "completed" and job.get("pipeline_json"):
            for child in self.database.pipeline_children(job_id):
                await self.events.publish({"type": "job.updated", "job": child})
        if (
            job and finished_from is not None and finished_from not in TERMINAL_STATUSES
            and job["status"] in TERMINAL_STATUSES
            and not job.get("task_id") and not job.get("consultation")
        ):
            # The side menu reads a folder chat's state from chat.updated.
            await self._publish_chat_update(job["chat_id"])

    @staticmethod
    def _message_result(
        messages: list[dict[str, Any]], message_id: str, *, task_session: bool = False
    ) -> tuple[bool, str | None]:
        user_index: int | None = None
        for index, message in enumerate(messages):
            info = message.get("info", message)
            if info.get("id") == message_id:
                user_index = index
                break
        if user_index is None:
            return False, None
        latest: dict[str, Any] | None = None
        parent_id = message_id
        for message in messages[user_index + 1 :]:
            info = message.get("info", message)
            if info.get("role") == "user":
                if not task_session:
                    break
                # Follow OpenCode-generated compaction/continuation messages
                # after the gateway prompt for the current task phase.
                parent_id = info.get("id")
                latest = None
                continue
            if info.get("role") != "assistant" or info.get("parentID", parent_id) != parent_id:
                continue
            if info.get("error") is not None or info.get("finish") in {"error", "content-filter"}:
                return True, "failed"
            latest = info
        if latest and not latest.get("summary"):
            finish = latest.get("finish")
            if finish not in {"tool-calls", "unknown"} and (
                finish or (latest.get("time") or {}).get("completed")
            ):
                return True, "completed"
        return True, None

    @staticmethod
    def _question_answers(request: dict[str, Any]) -> list[list[str]]:
        """OpenCode answers contain the original option labels, one list per question."""

        def recommended(value: str) -> bool:
            normalized = value.casefold()
            return not re.search(r"\b(?:not\s+recommended|не\s+рекоменд\w*)", normalized) and bool(
                re.search(r"\b(?:recommended|рекоменд(?:у|о)\w*)\b", normalized)
            )

        questions = request.get("questions")
        if not isinstance(questions, list) or not questions:
            raise TaskExecutionError("question_without_options")
        answers: list[list[str]] = []
        for question in questions:
            options = [
                option
                for option in question.get("options", [])
                if isinstance(option, dict)
                and isinstance(option.get("label"), str)
                and option["label"].strip()
            ]
            if not options:
                raise TaskExecutionError("question_without_options")
            choices = [option for option in options if recommended(option["label"])]
            if not choices:
                choices = [
                    option for option in options if recommended(str(option.get("description", "")))
                ]
            choices = choices or options[:1]
            if not question.get("multiple"):
                choices = choices[:1]
            answers.append([option["label"] for option in choices])
        return answers

    async def _answer_task_questions(
        self,
        job: dict[str, Any],
        client: Any,
        answered: set[str],
        failures: dict[str, int],
    ) -> bool:
        pending = [
            request
            for request in await self._poll_opencode(
                job["id"], "questions", client.questions
            )
            if request.get("sessionID") == job["session_id"]
        ]
        for request in pending:
            request_id = request.get("id")
            if not isinstance(request_id, str) or not request_id:
                raise TaskExecutionError("invalid_question")
            if request_id in answered:
                continue
            current = self.database.get_job(job["id"])
            if not current or current["status"] in {
                "completed",
                "failed",
                "cancelled",
                "cancelling",
            }:
                return bool(pending)
            try:
                accepted = await client.respond_question(
                    request_id, self._question_answers(request)
                )
                if accepted is False:
                    raise OpenCodeError("OpenCode did not accept the question reply")
            except OpenCodeError:
                # A timeout or 404 can race with a successful reply. Only retry if
                # OpenCode still lists this request, without resending the prompt.
                remaining = await self._poll_opencode(
                    job["id"], "questions-after-reply", client.questions
                )
                if any(item.get("id") == request_id for item in remaining):
                    failures[request_id] = failures.get(request_id, 0) + 1
                    if failures[request_id] >= 3:
                        raise TaskExecutionError("question_reply_failed") from None
                    continue
            answered.add(request_id)
            failures.pop(request_id, None)
        return bool(pending)

    @staticmethod
    def _busy(status: Any) -> bool:
        if status is None:
            return False
        if isinstance(status, str):
            return status.lower() not in {"idle", "completed", "failed", "aborted"}
        if isinstance(status, dict):
            kind = status.get("type") or status.get("status") or status.get("state")
            return QueueDispatcher._busy(kind) if kind else bool(status)
        return bool(status)

    async def _execute(self, job: dict[str, Any], client: Any = None) -> None:
        job_id = job["id"]
        before: str | None = None
        try:
            # A pending session has nothing to abort yet. Allow cancellation to
            # finish while cold startup is still waiting for readiness.
            prepared_client = None
            current = self.database.get_job(job_id)
            if current:
                before = current["status"]
            if (client is None and current and current.get("session_pending")
                    and current["status"] == "dispatching"):
                prepared_client = await self.manager.ensure(job["workspace_snapshot"])
            async with self._job_lock(job_id):
                current = self.database.get_job(job_id)
                if not current or current["status"] in {
                    "completed", "failed", "cancelled", "cancelling",
                }:
                    return
                if client is None:
                    client = prepared_client or await self.manager.ensure(job["workspace_snapshot"])
                    current = self.database.get_job(job_id)
                    if not current or current["status"] != "dispatching":
                        return
                    job = current
                    if job.get("resume_monitor"):
                        if job.get("session_pending"):
                            raise TaskExecutionError("invalid_monitor_resume")
                    else:
                        if job.get("session_pending"):
                            session = await client.create_session(
                                title=job.get("task_initial_title") or (
                                    job["chat_title"] if job.get("consultation") else None
                                ),
                                **({"permission": CONSULTATION_PERMISSIONS}
                                   if job.get("consultation") else {}),
                            )
                            session_id = session.get("id") or session.get("sessionID")
                            if not session_id:
                                raise OpenCodeError("OpenCode returned no session ID")
                            if (job.get("consultation") and session.get(
                                    "permission", []) != CONSULTATION_PERMISSIONS):
                                with suppress(OpenCodeError):
                                    await client.delete_session(session_id)
                                raise TaskExecutionError("consultation_permissions_unavailable")
                            if not self.database.attach_task_session(job_id, session_id):
                                await client.delete_session(session_id)
                                return
                            job = self.database.get_job(job_id)
                        elif job.get("consultation"):
                            # Older consultations persisted a deny-by-default ruleset.
                            # PATCH appends rules rather than clearing them. Override
                            # old restrictions with the actual configured agent rules,
                            # refreshing them when configuration changes. New sessions
                            # inherit directly. Recovery of active jobs skips this path.
                            session = await client.session(job["session_id"])
                            if session.get("permission"):
                                standard = next(
                                    (agent.get("permission") for agent in await client.agents()
                                     if agent.get("name") == job["agent"]), None,
                                )
                                if not standard:
                                    raise TaskExecutionError(
                                        "consultation_permissions_unavailable"
                                    )
                                if session["permission"][-len(standard):] != standard:
                                    session = await client.update_session_permissions(
                                        job["session_id"], standard,
                                    )
                                    if session.get("permission", [])[-len(standard):] != standard:
                                        raise TaskExecutionError(
                                            "consultation_permissions_unavailable"
                                        )
                        current = self.database.get_job(job_id)
                        if not current or current["status"] != "dispatching":
                            return
                        await client.send_prompt_async(
                            job["session_id"],
                            message_id=job.get("phase2_message_id") or job["message_id"],
                            content=job.get("resume_content") or job["content"],
                            agent="plan" if job.get("plan_first") and not job.get(
                                "phase2_message_id"
                            ) else job["agent"],
                            provider_id=job["provider_id"],
                            model_id=job["model_id"],
                            model_variant=job["model_variant"],
                            **({"system": CONSULTATION_SYSTEM}
                               if job.get("consultation") else {}),
                        )
                self.database.set_job_status(job_id, "running")
            await self.events.publish({"type": "job.updated", "job": self.database.get_job(job_id)})
            await self._monitor(job, client)
        except asyncio.CancelledError:
            raise
        except TaskExecutionError as exc:
            current = self.database.get_job(job_id)
            if current and current["status"] not in {"completed", "failed", "cancelled"}:
                self.database.set_job_status(job_id, "failed", str(exc))
        except OpenCodeError:
            logger.exception("OpenCode job failed", extra={"job_id": job_id})
            current = self.database.get_job(job_id)
            if current and current["status"] not in {"completed", "failed", "cancelled"}:
                self.database.set_job_status(job_id, "failed", "opencode_unavailable")
        except Exception:
            logger.exception("Unexpected queue worker error", extra={"job_id": job_id})
            current = self.database.get_job(job_id)
            if current and current["status"] not in {"completed", "failed", "cancelled"}:
                self.database.set_job_status(job_id, "failed", "internal_error")
        finally:
            current = self.database.get_job(job_id)
            if (job.get("task_id") or job.get("consultation")) and (
                client and current and current["status"] == "failed"
                and not current.get("session_pending")
            ):
                # Release a hidden interaction or generation after a terminal failure.
                with suppress(OpenCodeError):
                    await client.abort(job["session_id"])
            # Only a stop cancels a job, and the stop reports that end itself.
            ended_here = current and current["status"] != "cancelled"
            await self._publish_job_update(job_id, finished_from=before if ended_here else None)
            self.wake()

    async def _start_build_phase(self, job: dict[str, Any], client: Any) -> dict[str, Any] | None:
        async with self._job_lock(job["id"]):
            current = self.database.get_job(job["id"])
            if not current or current["status"] != "running":
                return None
            if current.get("phase2_message_id"):
                return current
            message_id = self.database.reserve_build_phase(job["id"])
            if not message_id:
                return None
            # There is no await between the final status check/reservation and
            # submission. Cancellation aborts this same session under this lock.
            try:
                await client.send_prompt_async(
                    current["session_id"], message_id=message_id,
                    content=CONTINUE_PROMPT, agent=current["agent"],
                    provider_id=current["provider_id"], model_id=current["model_id"],
                    model_variant=current["model_variant"],
                )
            except OpenCodeError:
                found, _ = self._message_result(
                    await client.messages(current["session_id"]), message_id, task_session=True
                )
                if not found:
                    raise TaskExecutionError("build_submission_uncertain") from None
            current = self.database.get_job(job["id"])
        await self._publish_job_update(job["id"])
        return current

    async def _monitor(self, job: dict[str, Any], client: Any) -> None:
        job_id = job["id"]
        idle_polls = 0
        live_status = "running"
        is_task = bool(job.get("task_id"))
        is_consultation = bool(job.get("consultation"))
        answered: set[str] = set()
        failures: dict[str, int] = {}
        previous_questions: set[str] = set()
        # A model outage may last longer than a day. The LLM proxy retries the
        # individual provider request, while this monitor keeps the durable job
        # active until it completes or a user cancels it.
        while not self._stopping:
            current = self.database.get_job(job_id)
            if not current or current["status"] in {
                "completed", "failed", "cancelled", "cancelling",
            }:
                return
            statuses = await self._poll_opencode(
                job_id, "session-statuses", client.session_statuses
            )
            busy = self._busy(statuses.get(job["session_id"]))
            pending_question = False
            next_status: str | None = None
            chat_changed = False
            if is_task:
                async with self._job_lock(job_id):
                    pending_question = await self._answer_task_questions(
                        job, client, answered, failures
                    )
            elif is_consultation:
                pending = await self._poll_opencode(
                    job_id,
                    "consultation-questions",
                    partial(
                        session_questions, client, job["session_id"], job["workspace_snapshot"]
                    ),
                )
                if pending:
                    cleared = self.database.set_job_status(job_id, "waiting_question")
                    await self._publish_job_update(job_id)
                    if cleared:
                        await self._publish_chat_update(job["chat_id"])
                    return
            else:
                pending = await self._poll_opencode(
                    job_id,
                    "chat-questions",
                    partial(
                        session_questions, client, job["session_id"], job["workspace_snapshot"]
                    ),
                )
                pending_question = bool(pending)
                question_ids = {item["id"] for item in pending}
                if question_ids != previous_questions:
                    previous_questions = question_ids
                    await self.events.publish(
                        question_event(job["workspace_snapshot"], job["session_id"])
                    )
                    # Every new question needs the user, so a snooze ends here too.
                    if question_ids and self.database.clear_chat_snooze(job["chat_id"]):
                        chat_changed = True
                if pending_question:
                    next_status = "waiting_question"
                elif live_status == "waiting_question" and not busy:
                    # Answered; the agent finishes or goes idle on the next polls.
                    next_status = "running"
            idle_polls = 0 if busy or pending_question else idle_polls + 1
            if busy or is_task or is_consultation:
                permissions = await self._poll_opencode(
                    job_id, "permissions", client.permissions
                )
                waiting = any(
                    request.get("sessionID") == job["session_id"] for request in permissions
                )
                if (is_task or is_consultation) and waiting:
                    raise TaskExecutionError("permission_required")
                next_status = next_status or ("waiting_permission" if waiting else "running")
            if next_status:
                current = self.database.get_job(job_id)
                if not current or current["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                    "cancelling",
                }:
                    return
                if next_status != live_status or current["status"] != next_status:
                    live_status = next_status
                    cleared = self.database.set_job_status(job_id, next_status)
                    await self.events.publish(
                        {"type": "job.updated", "job": self.database.get_job(job_id)}
                    )
                    # The side menu reads a folder chat's waiting state from chat.updated.
                    folder = not is_task and not is_consultation
                    chat_changed = chat_changed or cleared or folder
            if chat_changed:
                await self._publish_chat_update(job["chat_id"])
            # prompt_async can return before its busy transition is visible.
            # Consecutive idle polls also cover very fast completed prompts.
            if idle_polls >= 3:
                if is_consultation:
                    answer = final_answer(
                        await client.messages(job["session_id"]), job["message_id"]
                    )
                    if not answer:
                        raise TaskExecutionError("incomplete_response")
                    self.database.complete_consultation_run(job_id, answer)
                if is_task:
                    messages = await client.messages(job["session_id"])
                    _, result = self._message_result(
                        messages,
                        job.get("phase2_message_id") or job["message_id"],
                        task_session=True,
                    )
                    if result != "completed":
                        raise TaskExecutionError(
                            "assistant_error" if result == "failed" else "incomplete_response"
                        )
                if is_task and job.get("plan_first") and not job.get("phase2_message_id"):
                    job = await self._start_build_phase(job, client)
                    if not job:
                        return
                    idle_polls = 0
                    continue
                if is_task:
                    self.database.complete_task_run(job_id, task_final_answer(
                        messages, job.get("phase2_message_id") or job["message_id"]
                    ))
                elif not is_consultation:
                    self.database.set_job_status(job_id, "completed")
                return
            await asyncio.sleep(2)
