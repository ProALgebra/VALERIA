import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.consultations import CONSULTATION_PERMISSIONS
from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.opencode_client import OpenCodeClient, OpenCodeError


@pytest.fixture
async def accepted(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        folder = workspace / "worker"
        folder.mkdir()
        user = db.create_user("worker", "long-enough-password", "user", str(folder))
        admin = db.get_user_by_login("admin")
        task = db.create_task(
            admin["id"], "Task", "", "Instructions", "build", "test", "model", None, True,
        )
        db.set_consultation_model("test/model", admin["id"])
        manager = SimpleNamespace(ensure=AsyncMock(side_effect=AssertionError("Must not start")))
        app.state.manager = manager
        app.state.dispatcher.manager = manager
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/auth/login", json={"login": "worker", "password": "long-enough-password"},
            )
            client.headers["x-csrf-token"] = response.json()["csrf_token"]
            yield SimpleNamespace(
                app=app, db=db, user=user, task=task, client=client, manager=manager,
                runner=app.state.dispatcher,
            )


def endpoint(t, kind):
    return {
        "chat": ("/api/chats", {"message": "Question"}, 201),
        "consultation": ("/api/consultations", {"message": "Question"}, 201),
        "task": (f"/api/tasks/{t.task['id']}/runs", {"user_input": "Question"}, 202),
    }[kind]


@pytest.mark.parametrize("kind", ["chat", "consultation", "task"])
async def test_acceptance_and_retry_are_durable_without_opencode(accepted, kind):
    t = accepted
    path, body, status = endpoint(t, kind)
    headers = {"idempotency-key": "same-request"}
    first = await t.client.post(path, json=body, headers=headers)
    assert first.status_code == status, first.text
    job = t.db.list_jobs()[0]
    assert job["status"] == "queued" and job["session_pending"] == 1
    assert t.db.get_chat(job["chat_id"])["opencode_session_id"] == job["session_id"]
    # A second connection observes committed acceptance; retries survive DB reopen.
    other = Database(t.db.path, t.db.session_secret)
    other.initialize()
    assert other.get_job(job["id"])["status"] == "queued"
    t.app.state.database = other
    try:
        object.__setattr__(t.app.state.settings, "max_queued_messages", 0)
        repeated = await t.client.post(path, json=body, headers=headers)
        assert repeated.status_code == status and repeated.json() == first.json()
        assert len(other.list_jobs()) == 1
        changed = {key: value + " changed" for key, value in body.items()}
        assert (await t.client.post(path, json=changed, headers=headers)).status_code == 409
    finally:
        t.app.state.database = t.db
        other.close()
    t.manager.ensure.assert_not_awaited()


@pytest.mark.parametrize("kind", ["chat", "consultation", "task"])
async def test_full_queue_rolls_back_chat_and_retry_key(accepted, kind):
    t = accepted
    path, body, status = endpoint(t, kind)
    object.__setattr__(t.app.state.settings, "max_queued_messages", 0)
    headers = {"idempotency-key": "retry-after-capacity"}
    assert (await t.client.post(path, json=body, headers=headers)).status_code == 429
    assert t.db._all("SELECT * FROM chats") == []
    assert t.db._all("SELECT * FROM submissions") == []
    object.__setattr__(t.app.state.settings, "max_queued_messages", 10)
    assert (await t.client.post(path, json=body, headers=headers)).status_code == status
    t.manager.ensure.assert_not_awaited()


async def test_pending_chat_pages_and_empty_chat_never_start_opencode(accepted):
    t = accepted
    response = await t.client.post("/api/chats", json={})
    chat = response.json()["chat"]
    assert response.json()["job"] is None
    assert t.db.list_jobs() == []
    base = f"/api/chats/{chat['id']}"
    for path in (base, base + "/messages", base + "/permissions", base + "/questions",
                 base + "/mcp", "/api/options?chat_id=" + chat["id"]):
        assert (await t.client.get(path)).status_code == 200
    await t.client.post(base + "/messages", json={"message": "First"})
    await t.client.post(base + "/messages", json={"message": "Second"})
    messages = (await t.client.get(base + "/messages")).json()
    assert [m["parts"][0]["text"] for m in messages] == ["First", "Second"]
    assert all(m["submission_status"] == "queued" for m in messages)
    t.manager.ensure.assert_not_awaited()


def upstream():
    return SimpleNamespace(
        create_session=AsyncMock(
            return_value={"id": "real", "permission": CONSULTATION_PERMISSIONS}
        ),
        delete_session=AsyncMock(return_value=True), send_prompt_async=AsyncMock(),
        abort=AsyncMock(return_value=True), session_statuses=AsyncMock(return_value={}),
    )


@pytest.mark.parametrize("kind", ["chat", "consultation", "task"])
async def test_worker_creates_session_after_claim_and_restart(accepted, kind):
    t = accepted
    path, body, _ = endpoint(t, kind)
    await t.client.post(path, json=body)
    first = t.db.claim_next_job(1)
    await t.runner.recover()
    assert t.db.get_job(first["id"])["status"] == "queued"
    t.manager.ensure.assert_not_awaited()
    client = upstream()
    t.manager.ensure.side_effect = None
    t.manager.ensure.return_value = client
    t.runner._monitor = AsyncMock()
    await t.runner._execute(t.db.claim_next_job(1))
    job = t.db.get_job(first["id"])
    assert job["session_pending"] == 0 and job["session_id"] == "real"
    assert job["status"] == "running"
    assert t.db.get_chat(job["chat_id"])["status"] == "running"
    assert t.db.get_chat(job["chat_id"])["opencode_session_id"] == "real"
    assert job["session_creation_started"] == 0
    assert client.create_session.await_args.kwargs["creation_key"] == first["id"]
    assert client.create_session.await_args.kwargs["recover_only"] is False
    assert client.send_prompt_async.await_args.args == ("real",)
    if kind == "consultation":
        assert client.create_session.await_args.kwargs["permission"] == []
        assert client.send_prompt_async.await_args.kwargs["agent"] == "build"
    else:
        assert "permission" not in client.create_session.await_args.kwargs
        assert "system" not in client.send_prompt_async.await_args.kwargs
    if kind == "task":
        assert client.create_session.await_args.kwargs["title"] == "Question"


@pytest.mark.parametrize("kind", ["chat", "consultation", "task"])
async def test_interrupted_session_creation_recovers_same_key_without_new_post(accepted, kind):
    t = accepted
    path, body, _ = endpoint(t, kind)
    await t.client.post(path, json=body)
    job = t.db.claim_next_job(1)
    client = upstream()
    t.manager.ensure.side_effect = None
    t.manager.ensure.return_value = client
    calls = []

    async def create_session(**kwargs):
        if not kwargs["recover_only"]:
            assert kwargs["creation_state"](True)
        # A separate connection must observe the creation marker before any POST.
        observer = Database(t.db.path, t.db.session_secret)
        try:
            assert observer.get_job(job["id"])["session_creation_started"] == 1
        finally:
            observer.close()
        calls.append(kwargs)
        if len(calls) == 1:
            raise asyncio.CancelledError
        assert kwargs["recover_only"] is True
        return {"id": "reconciled-session", "permission": CONSULTATION_PERMISSIONS}

    client.create_session.side_effect = create_session
    with pytest.raises(asyncio.CancelledError):
        await t.runner._execute(job)
    assert calls[0]["recover_only"] is False
    assert t.db.get_job(job["id"])["status"] == "dispatching"
    client.send_prompt_async.assert_not_awaited()

    reopened = Database(t.db.path, t.db.session_secret)
    try:
        reopened.initialize()
        runner = QueueDispatcher(reopened, t.manager, t.runner.events, 1)
        runner._monitor = AsyncMock()
        await runner.recover()
        assert reopened.get_job(job["id"])["status"] == "queued"
        await runner._execute(reopened.claim_next_job(1))
        recovered = reopened.get_job(job["id"])
        assert recovered["status"] == "running"
        assert recovered["session_id"] == "reconciled-session"
        assert recovered["session_creation_started"] == 0
    finally:
        reopened.close()
    assert [call["creation_key"] for call in calls] == [job["id"], job["id"]]
    client.send_prompt_async.assert_awaited_once()
    client.delete_session.assert_not_awaited()


async def test_followup_turns_share_session_even_with_stale_chat_snapshot(accepted):
    t = accepted
    response = await t.client.post("/api/chats", json={"message": "First"})
    chat = response.json()["chat"]
    await t.client.post(f"/api/chats/{chat['id']}/messages", json={"message": "Second"})
    client = upstream()
    t.manager.ensure.side_effect = None
    t.manager.ensure.return_value = client
    t.runner._monitor = AsyncMock()
    first = t.db.claim_next_job(2)
    assert t.db.claim_next_job(2) is None
    await t.runner._execute(first)
    t.db.create_job(
        chat=chat, effective_user_id=t.user["id"], actor_user_id=t.user["id"],
        content="Third", agent="build", provider_id=None, model_id=None, model_variant=None,
    )
    assert t.db.get_chat(chat["id"])["status"] == "running"
    assert {j["session_id"] for j in t.db.list_jobs()} == {"real"}
    assert all(not j["session_pending"] for j in t.db.list_jobs())
    assert t.db.claim_next_job(2) is None
    t.db.set_job_status(first["id"], "completed")
    assert t.db.get_chat(chat["id"])["status"] == "queued"
    await t.runner._execute(t.db.claim_next_job(2))
    assert client.create_session.await_count == 1
    assert client.send_prompt_async.await_count == 2


async def test_cancel_during_cold_start_finishes_before_startup(accepted):
    t = accepted
    await t.client.post("/api/chats", json={"message": "First"})
    entered, release = asyncio.Event(), asyncio.Event()
    client = upstream()

    async def slow_start(workspace):
        entered.set()
        await release.wait()
        return client

    t.manager.ensure.side_effect = slow_start
    job = t.db.claim_next_job(1)
    worker = asyncio.create_task(t.runner._execute(job))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await t.runner.stop_job(job["id"])
        await asyncio.wait_for(t.runner._cancellations[job["id"]], 1)
        assert t.db.get_job(job["id"])["status"] == "cancelled"
        assert not worker.done()
    finally:
        release.set()
        await worker
    client.create_session.assert_not_awaited()
    client.send_prompt_async.assert_not_awaited()


async def test_cancel_during_session_creation_never_sends_prompt(accepted):
    t = accepted
    await t.client.post("/api/chats", json={"message": "First"})
    job = t.db.claim_next_job(1)
    client = upstream()

    async def create_session(**kwargs):
        await t.runner.stop_job(job["id"])
        return {"id": "unused"}

    client.create_session.side_effect = create_session
    t.manager.ensure.side_effect = None
    t.manager.ensure.return_value = client
    await t.runner._execute(job)
    await asyncio.gather(*tuple(t.runner._workers))
    assert t.db.get_job(job["id"])["status"] == "cancelled"
    client.delete_session.assert_awaited_once_with("unused")
    client.send_prompt_async.assert_not_awaited()


async def test_cancel_during_session_creation_backoff_releases_worker(accepted, monkeypatch):
    t = accepted
    await t.client.post("/api/chats", json={"message": "First"})
    job = t.db.claim_next_job(1)
    attempted = asyncio.Event()
    requests = []

    async def transport(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        attempted.set()
        raise httpx.RemoteProtocolError("Lost creation response", request=request)

    client = OpenCodeClient("http://upstream", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://upstream", transport=httpx.MockTransport(transport),
    )
    client.send_prompt_async = AsyncMock()
    client.abort = AsyncMock(return_value=True)
    t.manager.ensure.side_effect = None
    t.manager.ensure.return_value = client
    monkeypatch.setattr("opencode_web.opencode_client.OPENCODE_RETRY_INITIAL_SECONDS", 10800)
    worker = asyncio.create_task(t.runner._execute(job))
    try:
        await asyncio.wait_for(attempted.wait(), 1)
        await t.runner.stop_job(job["id"])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 1)
        await asyncio.gather(*tuple(t.runner._workers))
        assert t.db.get_job(job["id"])["status"] == "cancelled"
        assert [request.method for request in requests] == ["GET", "POST"]
        client.send_prompt_async.assert_not_awaited()
        client.abort.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_startup_failure_is_persisted_and_user_can_retry(accepted):
    t = accepted
    response = await t.client.post("/api/chats", json={"message": "First"})
    chat = response.json()["chat"]
    t.manager.ensure.side_effect = OpenCodeError("Unavailable")
    job = t.db.claim_next_job(1)
    await t.runner._execute(job)
    assert t.db.get_job(job["id"])["status"] == "failed"
    assert t.db.get_job(job["id"])["error_code"] == "opencode_unavailable"
    assert (await t.client.get(f"/api/chats/{chat['id']}/messages")).json()[0][
        "submission_status"
    ] == "failed"
    retry = await t.client.post(f"/api/chats/{chat['id']}/messages", json={"message": "First"})
    assert retry.status_code == 202 and retry.json()["session_pending"] == 1
