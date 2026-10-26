from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker
from opencode_web.opencode_client import OpenCodeError, OpenCodeReadTimeout


def question(request_id: str = "question-1", session_id: str = "session-task") -> dict[str, Any]:
    return {
        "id": request_id,
        "sessionID": session_id,
        "questions": [
            {
                "question": "Which approach?",
                "options": [{"label": "First"}, {"label": "Second (Recommended)"}],
            }
        ],
    }


def assistant(**extra: Any) -> dict[str, Any]:
    return {
        "info": {
            "id": "assistant-final",
            "role": "assistant",
            "parentID": "message-task",
            **extra,
        }
    }


class TaskQueue:
    def __init__(self) -> None:
        self.job: dict[str, Any] = {
            "id": "job-task",
            "chat_id": "chat-task",
            "task_id": "task-definition",
            "session_id": "session-task",
            "workspace_snapshot": "/workspace",
            "message_id": "message-task",
            "content": "Private admin prompt\n\nUser addition",
            "agent": "build",
            "provider_id": "provider",
            "model_id": "model",
            "model_variant": "high",
            "status": "dispatching",
            "error_code": None,
        }

    def active_jobs(self) -> list[dict[str, Any]]:
        return [dict(self.job)]

    def get_job(self, job_id: str) -> dict[str, Any]:
        assert job_id == self.job["id"]
        return dict(self.job)

    def set_job_status(self, job_id: str, status: str, error_code: str | None = None) -> None:
        assert job_id == self.job["id"]
        self.job.update(status=status, error_code=error_code)

    def requeue_interrupted_job(self, job_id: str) -> None:
        self.set_job_status(job_id, "queued")

    def clear_chat_snooze(self, chat_id: str) -> bool:
        return False

    def list_items(self, **_: Any) -> list[dict[str, Any]]:
        return []  # no chat rows, so a chat.updated is skipped

    def complete_task_run(self, job_id: str, answer: dict[str, Any] | None) -> None:
        self.set_job_status(job_id, "completed")
        self.job["result"] = answer


class TaskClient:
    def __init__(self) -> None:
        self.pending_questions: list[dict[str, Any]] = []
        self.pending_permissions: list[dict[str, Any]] = []
        self.message_list = [
            {"info": {"id": "message-task", "role": "user"}},
            assistant(finish="stop", time={"completed": 123}),
        ]
        self.prompts: list[dict[str, Any]] = []
        self.replies: list[tuple[str, list[list[str]]]] = []
        self.aborted: list[str] = []
        self.status_calls = 0
        self.question_calls = 0
        self.permission_calls = 0
        self.status_read_timeouts = 0
        self.question_read_timeouts = 0
        self.permission_read_timeouts = 0
        self.reply_failures = 0
        self.accept_then_error = False
        self.stale_reads = 0
        self.answered: set[str] = set()
        self.status_error_after: int | None = None

    async def send_prompt_async(self, session_id: str, **kwargs: Any) -> None:
        self.prompts.append({"session_id": session_id, **kwargs})

    async def session_statuses(self) -> dict[str, Any]:
        self.status_calls += 1
        if self.status_read_timeouts:
            self.status_read_timeouts -= 1
            raise OpenCodeReadTimeout("status timeout")
        if self.status_error_after and self.status_calls >= self.status_error_after:
            raise OpenCodeError("unavailable")
        return {}  # Questions can be outstanding while OpenCode reports idle.

    async def questions(self) -> list[dict[str, Any]]:
        self.question_calls += 1
        if self.question_read_timeouts:
            self.question_read_timeouts -= 1
            raise OpenCodeReadTimeout("question timeout")
        if self.answered:
            if self.stale_reads:
                self.stale_reads -= 1
            else:
                self.pending_questions = [
                    item for item in self.pending_questions if item["id"] not in self.answered
                ]
        return deepcopy(self.pending_questions)

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        self.replies.append((request_id, answers))
        if self.reply_failures:
            self.reply_failures -= 1
            raise OpenCodeError("transient failure")
        self.answered.add(request_id)
        if self.accept_then_error:
            raise OpenCodeError("reply accepted but HTTP response was lost")
        return True

    async def permissions(self) -> list[dict[str, Any]]:
        self.permission_calls += 1
        if self.permission_read_timeouts:
            self.permission_read_timeouts -= 1
            raise OpenCodeReadTimeout("permission timeout")
        return self.pending_permissions

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.message_list

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return True


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> tuple[QueueDispatcher, TaskQueue, TaskClient]:
    original_sleep = asyncio.sleep

    async def fast_poll(delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_poll)
    queue = TaskQueue()
    client = TaskClient()

    async def ensure(workspace: str) -> TaskClient:
        return client

    dispatcher = QueueDispatcher(queue, SimpleNamespace(ensure=ensure), EventBroker(), 1)
    return dispatcher, queue, client


@pytest.mark.parametrize("recover", [False, True])
async def test_plain_task_saves_final_text(runner: Any, recover: bool) -> None:
    dispatcher, queue, client = runner
    client.message_list[-1]["parts"] = [{"type": "text", "text": "Final result"}]
    if recover:
        queue.job["status"] = "running"
        await dispatcher.recover()
    else:
        await dispatcher._execute(dict(queue.job))
    assert queue.job["status"] == "completed"
    assert queue.job["result"] == {"message_id": "assistant-final", "text": "Final result"}


async def test_task_answers_every_question_once_and_sends_one_fixed_prompt(runner: Any) -> None:
    dispatcher, queue, client = runner
    request = question()
    request["questions"] += [
        {"options": [{"label": "Нет"}, {"label": "Да (Рекомендуется)"}]},
        {"options": [{"label": "Fallback"}, {"label": "Alternative"}]},
        {
            "multiple": True,
            "options": [
                {"label": "A (Recommended)"},
                {"label": "B"},
                {"label": "C (Recommended)"},
            ],
        },
    ]
    client.pending_questions = [request, question("foreign", "session-chat"), question("second")]
    client.stale_reads = 1

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "completed"
    assert client.replies == [
        (
            "question-1",
            [
                ["Second (Recommended)"],
                ["Да (Рекомендуется)"],
                ["Fallback"],
                ["A (Recommended)", "C (Recommended)"],
            ],
        ),
        ("second", [["Second (Recommended)"]]),
    ]
    assert client.prompts == [
        {
            "session_id": "session-task",
            "message_id": "message-task",
            "content": queue.job["content"],
            "agent": "build",
            "provider_id": "provider",
            "model_id": "model",
            "model_variant": "high",
        }
    ]
    assert client.pending_questions == [question("foreign", "session-chat")]
    assert client.aborted == []


@pytest.mark.parametrize(
    "label",
    ["Рекомендуемый вариант", "Option (RECOMMENDED)", "Рекомендовано", "Рекомендованный вариант"],
)
def test_recommended_markers(label: str) -> None:
    request = {
        "questions": [
            {
                "options": [
                    {"label": "Avoid (Not recommended)"},
                    {"label": label},
                ]
            }
        ]
    }
    assert QueueDispatcher._question_answers(request) == [[label]]


@pytest.mark.parametrize("negative", ["Не рекомендуется", "Не рекомендовано", "Не рекомендованный"])
def test_recommended_description_and_negative_russian_marker(negative: str) -> None:
    request = {
        "questions": [
            {
                "options": [
                    {"label": negative},
                    {"label": "Safe", "description": "Рекомендуется для этой задачи"},
                ]
            }
        ]
    }
    assert QueueDispatcher._question_answers(request) == [["Safe"]]


@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_question_reply_retries_are_bounded(runner: Any, failures: int) -> None:
    dispatcher, queue, client = runner
    client.pending_questions = [question()]
    client.reply_failures = failures

    await dispatcher._execute(dict(queue.job))

    assert len(client.prompts) == 1
    assert len(client.replies) == min(failures + 1, 3)
    if failures == 3:
        assert queue.job["status"] == "failed"
        assert queue.job["error_code"] == "question_reply_failed"
        assert client.aborted == ["session-task"]
    else:
        assert queue.job["status"] == "completed"


async def test_lost_reply_response_does_not_resend_an_accepted_answer(runner: Any) -> None:
    dispatcher, queue, client = runner
    client.pending_questions = [question()]
    client.accept_then_error = True

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "completed"
    assert len(client.replies) == 1


async def test_question_without_choices_fails_instead_of_waiting_forever(runner: Any) -> None:
    dispatcher, queue, client = runner
    client.pending_questions = [
        {
            "id": "question-freeform",
            "sessionID": "session-task",
            "questions": [{"question": "Enter text", "options": []}],
        }
    ]

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "failed"
    assert queue.job["error_code"] == "question_without_options"
    assert client.aborted == ["session-task"]


async def test_hidden_permission_request_fails_and_aborts_even_when_idle(runner: Any) -> None:
    dispatcher, queue, client = runner
    client.pending_permissions = [{"sessionID": "session-task", "id": "permission"}]

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "failed"
    assert queue.job["error_code"] == "permission_required"
    assert client.aborted == ["session-task"]


@pytest.mark.parametrize(
    "messages,error_code",
    [
        ([], "incomplete_response"),
        (
            [assistant(error={"name": "ProviderError", "data": {"message": "private"}})],
            "assistant_error",
        ),
        ([assistant(finish="tool-calls", time={"completed": 123})], "incomplete_response"),
        ([assistant(finish="stop"), assistant(id="unfinished")], "incomplete_response"),
        ([assistant(finish="stop", parentID="another-message")], "incomplete_response"),
    ],
)
async def test_task_requires_a_successful_completed_response(
    runner: Any,
    messages: list[dict[str, Any]],
    error_code: str,
) -> None:
    dispatcher, queue, client = runner
    client.message_list = client.message_list[:1] + messages

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "failed"
    assert queue.job["error_code"] == error_code
    assert client.aborted == ["session-task"]


async def test_task_accepts_final_response_after_tool_steps(runner: Any) -> None:
    dispatcher, queue, client = runner
    client.message_list.insert(1, assistant(finish="tool-calls", time={"completed": 1}))

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "completed"


@pytest.mark.parametrize("final_response", [False, True])
async def test_task_completion_follows_opencode_compaction_continuations(
    runner: Any,
    final_response: bool,
) -> None:
    dispatcher, queue, client = runner
    client.message_list = client.message_list[:1] + [
        assistant(finish="tool-calls", time={"completed": 1}),
        {
            "info": {"id": "compact", "role": "user"},
            "parts": [{"type": "compaction", "auto": True}],
        },
        assistant(id="summary", parentID="compact", summary=True, finish="stop"),
        {
            "info": {"id": "continue", "role": "user"},
            "parts": [{"type": "text", "synthetic": True, "text": "Continue"}],
        },
    ]
    if final_response:
        client.message_list.append(assistant(parentID="continue", finish="stop"))

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == ("completed" if final_response else "failed")
    assert len(client.prompts) == 1


async def test_ordinary_chat_keeps_interactive_questions(runner: Any) -> None:
    dispatcher, queue, client = runner
    queue.job["task_id"] = None
    client.pending_questions = [question()]

    worker = asyncio.create_task(dispatcher._execute(dict(queue.job)))
    while client.question_calls < 5:
        await asyncio.sleep(0)
    assert queue.job["status"] == "waiting_question"
    assert client.replies == []
    client.pending_questions.clear()  # The user answers through the chat endpoint.
    await worker
    assert queue.job["status"] == "completed"
    assert len(client.prompts) == 1


@pytest.mark.parametrize("permission", [False, True])
async def test_restart_resumes_idle_pending_task_without_resending_prompt(
    runner: Any,
    permission: bool,
) -> None:
    dispatcher, queue, client = runner
    queue.job["status"] = "running"
    if permission:
        client.pending_permissions = [{"sessionID": "session-task", "id": "permission"}]
    else:
        client.pending_questions = [question()]

    await dispatcher.recover()
    await asyncio.gather(*tuple(dispatcher._workers))

    assert client.prompts == []
    assert queue.job["status"] == ("failed" if permission else "completed")
    if permission:
        assert queue.job["error_code"] == "permission_required"
    else:
        assert len(client.replies) == 1


async def test_poll_read_timeout_retries_with_capped_exponential_backoff(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _, _ = runner
    delays: list[float] = []
    attempts = 0

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def request() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 7:
            raise OpenCodeReadTimeout("poll timeout")
        return "ready"

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", record_sleep)

    assert await dispatcher._poll_opencode("job-task", "status", request) == "ready"
    assert delays == [1, 2, 4, 8, 16, 30, 30]


async def test_task_survives_read_timeout_from_every_monitor_poll(runner: Any) -> None:
    dispatcher, queue, client = runner
    client.message_list[-1]["parts"] = [{"type": "text", "text": "Final result"}]
    client.status_read_timeouts = 1
    client.question_read_timeouts = 1
    client.permission_read_timeouts = 1

    await dispatcher._execute(dict(queue.job))

    assert queue.job["status"] == "completed"
    assert queue.job["error_code"] is None
    assert client.status_calls == 4
    assert client.question_calls == 4
    assert client.permission_calls == 4


async def test_recovery_survives_read_timeout_from_every_service_poll(runner: Any) -> None:
    dispatcher, queue, client = runner
    queue.job["status"] = "running"
    client.message_list[-1]["parts"] = [{"type": "text", "text": "Final result"}]
    client.status_read_timeouts = 1
    client.question_read_timeouts = 1
    client.permission_read_timeouts = 1

    await dispatcher.recover()

    assert queue.job["status"] == "completed"
    assert queue.job["error_code"] is None
    assert client.status_calls == 2
    assert client.question_calls == 2
    assert client.permission_calls == 2


async def test_recovered_worker_handles_failure_and_publishes_terminal_status(runner: Any) -> None:
    dispatcher, queue, client = runner
    queue.job["status"] = "running"
    client.pending_questions = [question()]
    client.status_error_after = 2

    await dispatcher.recover()
    await asyncio.gather(*tuple(dispatcher._workers))

    assert client.prompts == []
    assert queue.job["status"] == "failed"
    assert queue.job["error_code"] == "opencode_unavailable"
    assert dispatcher._wake.is_set()


@pytest.mark.parametrize(
    "status,has_user,expected",
    [
        ("dispatching", False, "queued"),
        ("dispatching", True, "failed"),
        ("running", True, "failed"),
    ],
)
async def test_restart_only_requeues_a_prompt_that_was_not_accepted(
    runner: Any,
    status: str,
    has_user: bool,
    expected: str,
) -> None:
    dispatcher, queue, client = runner
    queue.job["status"] = status
    client.message_list = client.message_list[:1] if has_user else []

    await dispatcher.recover()

    assert queue.job["status"] == expected
    assert client.prompts == []


async def test_restart_does_not_report_an_assistant_error_as_success(runner: Any) -> None:
    dispatcher, queue, client = runner
    queue.job["status"] = "running"
    client.message_list[-1] = assistant(error={"name": "ProviderError"}, time={"completed": 1})

    await dispatcher.recover()

    assert queue.job["status"] == "failed"
    assert queue.job["error_code"] == "assistant_error"


async def test_task_dispatch_uses_the_same_sqlite_fifo_and_fixed_settings(
    runner: Any,
    tmp_path: Path,
    workspace: Path,
) -> None:
    dispatcher, _, client = runner
    database = Database(tmp_path / "queue.sqlite", "s" * 32)
    database.initialize()
    dispatcher.database = database
    try:
        admin = database.create_user("admin", "long-enough-password", "admin", None)
        user = database.create_user("user", "long-enough-password", "user", str(workspace))
        task = database.create_task(
            admin["id"],
            "Task",
            "Description",
            "Private prompt",
            "build",
            "provider",
            "model",
            "high",
            True,
        )
        chat = database.create_chat(user["id"], "session-chat", "Chat", str(workspace))
        chat_job = database.create_job(
            chat=chat,
            effective_user_id=user["id"],
            actor_user_id=user["id"],
            content="Chat first",
            agent="plan",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        task_job = database.create_task_job(
            task=task,
            user=user,
            actor_user_id=user["id"],
            session_id="session-task",
            user_input="Extra input",
            max_queued=100,
        )
        assert database.claim_next_job(1)["id"] == chat_job["id"]
        assert database.claim_next_job(1) is None
        database.set_job_status(chat_job["id"], "completed")
        claimed = database.claim_next_job(1)
        assert claimed["id"] == task_job["id"]
        assert claimed["task_id"] == task["id"]
        client.message_list = [
            {"info": {"id": claimed["message_id"], "role": "user"}},
            assistant(parentID=claimed["message_id"], finish="stop"),
        ]
        client.pending_questions = [question()]

        await dispatcher._execute(claimed)

        assert database.get_job(task_job["id"])["status"] == "completed"
        assert database.get_job(task_job["id"])["finished_at"] is not None
        assert client.prompts[0]["content"] == "Private prompt\n\nExtra input"
        assert client.prompts[0]["agent"] == "build"
        assert client.prompts[0]["model_variant"] == "high"
        assert client.prompts[0]["model_id"] == "model"
        assert len(client.prompts) == 1
    finally:
        database.close()
