from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker


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
    from types import SimpleNamespace

    database = Database(tmp_path / "questions.sqlite", "x" * 32)
    database.initialize()
    job = active_job(database, workspace, "running")

    class QuestionClient(RecoveryClient):
        def __init__(self):
            super().__init__([])
            self.polls = 0

        async def questions(self):
            self.polls += 1
            status = database.get_job(job["id"])["status"]
            if self.polls <= 5:
                # Recovery and the first monitor poll find it running, then it waits.
                assert status == ("running" if self.polls <= 2 else "waiting_question")
                return [{"id": "que-1", "sessionID": job["session_id"], "questions": []}]
            if self.polls == 7:
                assert status == "running"  # answered; the agent carries on
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

    monkeypatch.setattr("opencode_web.dispatcher.asyncio", SimpleNamespace(sleep=no_delay))
    # The recovery client deliberately has no send_prompt_async: a resend fails this test.
    await spawned[0]
    assert client.polls == 8
    assert database.get_job(job["id"])["status"] == "completed"
    database.close()
