import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_backend_switch import enqueue
from test_backend_switch import stored as stored

from opencode_web.backends.contracts import OpenCodeSubmissionUncertain
from opencode_web.dispatcher import QueueDispatcher


def runner(database, client):
    return QueueDispatcher(
        database,
        SimpleNamespace(ensure=AsyncMock(return_value=client)),
        SimpleNamespace(publish=AsyncMock()),
        1,
    )


async def test_uncertain_admission_survives_restart_without_another_prompt(stored, monkeypatch):
    t = stored
    job = enqueue(t, t.chat)
    job = t.db.claim_next_job(1)
    observed = asyncio.Event()
    visible = False
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    async def history(session_id):
        observed.set()
        if not visible:
            return []
        return [
            {"info": {"id": job["message_id"], "role": "user"}, "parts": []},
            {
                "info": {
                    "id": "msg_answer",
                    "role": "assistant",
                    "parentID": job["message_id"],
                    "finish": "stop",
                },
                "parts": [],
            },
        ]

    client = SimpleNamespace(
        send_prompt_async=AsyncMock(side_effect=OpenCodeSubmissionUncertain("lost receipt")),
        messages=history,
        questions=AsyncMock(return_value=[]),
        permissions=AsyncMock(return_value=[]),
        session_statuses=AsyncMock(return_value={}),
        abort=AsyncMock(return_value=True),
    )
    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_sleep)
    first = runner(t.db, client)
    worker = asyncio.create_task(first._execute(job))
    await asyncio.wait_for(observed.wait(), 2)
    assert t.db.get_job(job["id"])["error_code"] == "submission_uncertain"
    assert t.db.claim_next_job(1) is None
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    observed.clear()
    restarted = runner(t.db, client)
    try:
        await restarted.recover()
        await asyncio.wait_for(observed.wait(), 2)
        assert t.db.get_job(job["id"])["error_code"] == "submission_uncertain"
        visible = True
        await asyncio.wait_for(asyncio.gather(*restarted._workers), 2)
        assert t.db.get_job(job["id"])["status"] == "completed"
        client.send_prompt_async.assert_awaited_once()
    finally:
        await restarted.stop()


async def test_cancellation_keeps_slot_until_uncertain_input_is_located(stored, monkeypatch):
    t = stored
    job = enqueue(t, t.chat)
    t.db.claim_next_job(1)
    t.db.set_job_status(job["id"], "running", "submission_uncertain")
    client = SimpleNamespace(
        messages=AsyncMock(return_value=[]),
        abort=AsyncMock(return_value=True),
        session_statuses=AsyncMock(return_value={}),
    )
    dispatcher = runner(t.db, client)
    seen = asyncio.Event()
    original_sleep = asyncio.sleep

    async def pause(delay):
        seen.set()
        await original_sleep(100)

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", pause)
    try:
        await dispatcher.stop_job(job["id"])
        await asyncio.wait_for(seen.wait(), 2)
        uncertain = t.db.get_job(job["id"])
        assert uncertain["status"] == "cancelling"
        assert uncertain["error_code"] == "submission_uncertain"
        assert t.db.active_jobs()
        client.abort.assert_not_awaited()
    finally:
        await dispatcher.stop()
    # Once admission is visible, ordinary abort can be confirmed and free the slot.
    client.messages.return_value = [{"info": {"id": job["message_id"]}, "parts": []}]
    dispatcher._stopping = False
    await dispatcher._cancel_job(job["id"])
    assert t.db.get_job(job["id"])["status"] == "cancelled"
    client.abort.assert_awaited_once()
