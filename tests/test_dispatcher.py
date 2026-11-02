from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker
from opencode_web.opencode_client import OpenCodeTransportError


class RecoveryClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    async def session_statuses(self) -> dict[str, Any]:
        return {}

    async def questions(self) -> list[dict[str, Any]]:
        return []

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self._messages

    async def abort(self, session_id: str) -> bool:
        return True


class RecoveryManager:
    def __init__(self, client: RecoveryClient) -> None:
        self.client = client

    async def ensure(self, workspace: str) -> RecoveryClient:
        return self.client


def active_job(database: Database, workspace: Path, status: str) -> dict[str, Any]:
    user = database.create_user("worker", "long-enough-password", "user", str(workspace))
    chat = database.create_chat(user["id"], "recovery-session", "Recovery", str(workspace))
    job = database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="continue safely",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    database.claim_next_job(1)
    if status != "dispatching":
        database.set_job_status(job["id"], status)
    return database.get_job(job["id"])


@pytest.mark.parametrize(
    ("initial_status", "messages", "expected_status", "error_code"),
    [
        ("dispatching", [], "queued", None),
        (
            "running",
            [
                {"info": {"id": "MESSAGE_ID", "role": "user"}},
                {
                    "info": {
                        "id": "assistant",
                        "role": "assistant",
                        "parentID": "MESSAGE_ID",
                        "time": {"completed": 123},
                    }
                },
            ],
            "completed",
            None,
        ),
        (
            "running",
            [{"info": {"id": "MESSAGE_ID", "role": "user"}}],
            "failed",
            "interrupted_by_restart",
        ),
    ],
)
async def test_recovery_reconciles_interrupted_jobs(
    tmp_path: Path,
    workspace: Path,
    initial_status: str,
    messages: list[dict[str, Any]],
    expected_status: str,
    error_code: str | None,
) -> None:
    database = Database(tmp_path / f"{initial_status}.sqlite", "x" * 32)
    database.initialize()
    job = active_job(database, workspace, initial_status)
    for message in messages:
        info = message["info"]
        if info.get("id") == "MESSAGE_ID":
            info["id"] = job["message_id"]
        if info.get("parentID") == "MESSAGE_ID":
            info["parentID"] = job["message_id"]

    dispatcher = QueueDispatcher(
        database, RecoveryManager(RecoveryClient(messages)), EventBroker(), 1
    )
    await dispatcher.recover()

    recovered = database.get_job(job["id"])
    assert recovered["status"] == expected_status
    assert recovered["error_code"] == error_code
    database.close()


async def test_chat_question_keeps_idle_job_active_and_recovery_does_not_resend(
    tmp_path, workspace, monkeypatch
):
    database = Database(tmp_path / "questions.sqlite", "x" * 32)
    database.initialize()
    job = active_job(database, workspace, "running")

    class QuestionClient(RecoveryClient):
        def __init__(self):
            super().__init__([])
            self.polls = 0

        async def questions(self):
            self.polls += 1
            if self.polls <= 5:
                assert database.get_job(job["id"])["status"] == "running"
                return [{"id": "que-1", "sessionID": job["session_id"], "questions": []}]
            return []

    client = QuestionClient()
    dispatcher = QueueDispatcher(database, RecoveryManager(client), EventBroker(), 1)
    spawned = []

    def capture(coroutine, name):
        spawned.append(coroutine)

    monkeypatch.setattr(dispatcher, "_spawn", capture)
    await dispatcher.recover()
    assert len(spawned) == 1
    assert database.get_job(job["id"])["status"] == "running"

    async def no_delay(seconds):
        return None

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", no_delay)
    # The recovery client deliberately has no send_prompt_async: a resend fails this test.
    await spawned[0]
    assert client.polls == 8
    assert database.get_job(job["id"])["status"] == "completed"
    database.close()


async def test_cancelling_recovery_retry_does_not_stop_reconciling_later_jobs(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "cancel-recovery.sqlite", "x" * 32)
    database.initialize()
    first = active_job(database, workspace, "running")
    chat = database.create_chat(
        first["effective_user_id"], "second-recovery-session", "Second recovery", str(workspace)
    )
    second = database.create_job(
        chat=chat,
        effective_user_id=first["effective_user_id"],
        actor_user_id=first["actor_user_id"],
        content="complete independently",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    assert database.claim_next_job(2)["id"] == second["id"]
    database.set_job_status(second["id"], "running")
    messages = [
        {"info": {"id": second["message_id"], "role": "user"}},
        {
            "info": {
                "id": "second-assistant",
                "role": "assistant",
                "parentID": second["message_id"],
                "finish": "stop",
            }
        },
    ]
    retry_started = asyncio.Event()
    wait_forever = asyncio.Event()

    class DisconnectingClient(RecoveryClient):
        async def session_statuses(self) -> dict[str, Any]:
            if database.get_job(first["id"])["status"] == "running":
                raise OpenCodeTransportError("recovery connection lost")
            return {}

    async def wait_in_backoff(delay: float) -> None:
        retry_started.set()
        await wait_forever.wait()

    client = DisconnectingClient(messages)
    dispatcher = QueueDispatcher(database, RecoveryManager(client), EventBroker(), 2)
    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", wait_in_backoff)
    recovering = asyncio.create_task(dispatcher.recover())
    try:
        await asyncio.wait_for(retry_started.wait(), timeout=1)

        await dispatcher.stop_job(first["id"])
        await asyncio.wait_for(recovering, timeout=1)
        await asyncio.wait_for(asyncio.gather(*tuple(dispatcher._workers)), timeout=1)

        assert database.get_job(first["id"])["status"] == "cancelled"
        assert database.get_job(second["id"])["status"] == "completed"
    finally:
        recovering.cancel()
        await asyncio.gather(recovering, return_exceptions=True)
        await dispatcher.stop()
        database.close()


async def test_startup_recovery_outage_does_not_block_available_queue_capacity(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "independent-recovery.sqlite", "x" * 32)
    database.initialize()
    interrupted = active_job(database, workspace, "running")
    healthy_workspace = tmp_path / "healthy-workspace"
    healthy_workspace.mkdir()
    user = database.create_user(
        "healthy-worker", "long-enough-password", "user", str(healthy_workspace)
    )
    chat = database.create_chat(
        user["id"], "healthy-session", "Healthy workspace", str(healthy_workspace)
    )
    queued = database.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="use remaining capacity",
        agent="build",
        provider_id=None,
        model_id=None,
        model_variant=None,
    )
    recovery_retrying = asyncio.Event()
    prompt_submitted = asyncio.Event()

    class UnavailableClient(RecoveryClient):
        async def session_statuses(self) -> dict[str, Any]:
            recovery_retrying.set()
            raise OpenCodeTransportError("workspace is temporarily unavailable")

    class HealthyClient(RecoveryClient):
        async def send_prompt_async(self, session_id: str, **kwargs: Any) -> None:
            assert session_id == queued["session_id"]
            prompt_submitted.set()

        async def session_statuses(self) -> dict[str, Any]:
            return {queued["session_id"]: {"type": "busy"}}

        async def permissions(self) -> list[dict[str, Any]]:
            return []

    unavailable = UnavailableClient([])
    healthy = HealthyClient([])

    class IndependentManager:
        async def ensure(self, path: str) -> RecoveryClient:
            return unavailable if path == str(workspace) else healthy

    dispatcher = QueueDispatcher(database, IndependentManager(), EventBroker(), 2)
    original_recover = dispatcher.recover
    recovering_job_ids: list[str] = []

    async def record_recovery(jobs: list[dict[str, Any]] | None = None) -> None:
        assert jobs is not None
        recovering_job_ids.extend(job["id"] for job in jobs)
        await original_recover(jobs)

    monkeypatch.setattr(dispatcher, "recover", record_recovery)
    dispatcher.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(recovery_retrying.wait(), prompt_submitted.wait()), timeout=1
        )
        recovering = next(
            worker for worker in dispatcher._workers if worker.get_name() == "queue-recovery"
        )
        assert not recovering.done()
        assert recovering_job_ids == [interrupted["id"]]
        assert database.get_job(interrupted["id"])["status"] == "running"
        assert database.get_job(queued["id"])["status"] == "running"

        await dispatcher.stop()

        assert recovering.cancelled()
        assert not dispatcher._workers
        assert database.get_job(interrupted["id"])["status"] == "running"
    finally:
        await dispatcher.stop()
        database.close()
