import asyncio
import json

import httpx
import pytest

from config import Settings
from valeria_client import ValeriaClient, ValeriaError

TASK = {"id": "task-1", "title": "Build report", "description": "Available task"}
RUN = {"id": "run-1", "task_id": TASK["id"], "status": "queued"}
RESULT = {"run_id": RUN["id"], "status": "not_completed", "text": None}


def settings():
    return Settings(
        url="https://valeria.example/team/",
        login="operator",
        password="private-login-password",
    )


def login_response(generation=1):
    return httpx.Response(
        200,
        headers={"set-cookie": f"valeria_session=session-{generation}; Path=/; HttpOnly"},
        json={"user": {"id": "user-1"}, "csrf_token": f"csrf-{generation}"},
    )


async def test_user_task_workflow_uses_session_cookie_csrf_and_base_path():
    requests = []
    replies = {
        ("GET", "/team/api/tasks"): [TASK],
        ("POST", "/team/api/tasks/task-1/runs"): RUN,
        ("GET", "/team/api/task-runs"): [RUN],
        ("GET", "/team/api/task-runs/run-1"): RUN,
        ("GET", "/team/api/task-runs/run-1/result"): RESULT,
    }

    def handler(request):
        requests.append(request)
        if request.url.path == "/team/api/auth/login":
            assert json.loads(request.content) == {
                "login": "operator",
                "password": "private-login-password",
            }
            return login_response()
        assert request.headers["cookie"] == "valeria_session=session-1"
        if request.method == "POST":
            assert request.headers["x-csrf-token"] == "csrf-1"
            assert request.headers["idempotency-key"] == "launch-report-1"
            assert json.loads(request.content) == {"user_input": "November"}
        return httpx.Response(
            202 if request.method == "POST" else 200,
            json=replies[(request.method, request.url.path)],
        )

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tasks() == [TASK]
        assert (
            await client.create_task_run("task-1", "November", idempotency_key="launch-report-1")
            == RUN
        )
        assert await client.list_task_runs() == [RUN]
        assert await client.get_task_run("run-1") == RUN
        assert await client.get_task_result("run-1") == RESULT

    assert sum(request.url.path.endswith("/auth/login") for request in requests) == 1
    assert len(requests) == 6


async def test_concurrent_first_requests_share_one_login():
    login_started = asyncio.Event()
    finish_login = asyncio.Event()
    login_count = 0

    async def handler(request):
        nonlocal login_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            login_started.set()
            await finish_login.wait()
            return login_response()
        assert request.headers["cookie"] == "valeria_session=session-1"
        return httpx.Response(200, json=[TASK])

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        calls = [asyncio.create_task(client.list_tasks()) for _ in range(5)]
        await asyncio.wait_for(login_started.wait(), timeout=2)
        finish_login.set()
        assert await asyncio.gather(*calls) == [[TASK]] * 5

    assert login_count == 1


async def test_expired_session_relogs_once_and_reuses_submission_key_and_body():
    login_count = 0
    submissions = []

    def handler(request):
        nonlocal login_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            return login_response(login_count)
        submissions.append(request)
        if len(submissions) == 1:
            return httpx.Response(401, json={"detail": "Session expired"})
        assert request.headers["cookie"] == "valeria_session=session-2"
        assert request.headers["x-csrf-token"] == "csrf-2"
        return httpx.Response(202, json=RUN)

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        assert (
            await client.create_task_run("task-1", "some input", idempotency_key="stable-retry-key")
            == RUN
        )

    assert login_count == 2
    assert len(submissions) == 2
    assert [request.headers["idempotency-key"] for request in submissions] == [
        "stable-retry-key",
        "stable-retry-key",
    ]
    assert submissions[0].content == submissions[1].content


async def test_repeated_unauthorized_response_stops_after_one_relogin():
    login_count = 0
    request_count = 0

    def handler(request):
        nonlocal login_count, request_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            return login_response(login_count)
        request_count += 1
        return httpx.Response(401, text="private-response-body")

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.list_tasks()

    assert login_count == 2
    assert request_count == 2
    assert caught.value.status_code == 401
    assert "private-response-body" not in str(caught.value)
    assert settings().password not in str(caught.value)


async def test_ambiguous_submission_is_not_retried_and_reports_the_reusable_key():
    submission_count = 0

    def handler(request):
        nonlocal submission_count
        if request.url.path.endswith("/auth/login"):
            return login_response()
        submission_count += 1
        raise httpx.ReadTimeout("private-transport-detail", request=request)

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.create_task_run("task-1", idempotency_key="recoverable-key")

    assert submission_count == 1
    assert caught.value.status_code is None
    assert caught.value.idempotency_key == "recoverable-key"
    assert "private-transport-detail" not in str(caught.value)
    assert settings().password not in str(caught.value)


@pytest.mark.parametrize("status", [403, 404, 409, 429, 500])
async def test_api_errors_keep_status_without_exposing_upstream_response(status):
    def handler(request):
        if request.url.path.endswith("/auth/login"):
            return login_response()
        return httpx.Response(status, json={"detail": "private-response-body"})

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.get_task_run("run-1")

    assert caught.value.status_code == status
    assert "private-response-body" not in str(caught.value)


async def test_failed_login_does_not_send_a_task_request_or_leak_credentials():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(401, text="private-response-body")

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.list_tasks()

    assert paths == ["/team/api/auth/login"]
    assert caught.value.status_code == 401
    assert "private-response-body" not in str(caught.value)
    assert settings().password not in str(caught.value)


@pytest.mark.parametrize("identifier", ["", " ", "../auth/logout", "task/other", "a?x=1"])
@pytest.mark.parametrize("method", ["create_task_run", "get_task_run", "get_task_result"])
async def test_invalid_identifiers_are_rejected_before_login(identifier, method):
    requests = []

    def handler(request):
        requests.append(request)
        return login_response()

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        kwargs = {"idempotency_key": "valid-key"} if method == "create_task_run" else {}
        with pytest.raises((ValueError, ValeriaError)):
            await getattr(client, method)(identifier, **kwargs)

    assert requests == []


@pytest.mark.parametrize("key", ["", " ", "bad\nheader", "ключ", "a" * 201])
async def test_invalid_idempotency_keys_are_rejected_before_login(key):
    requests = []

    def handler(request):
        requests.append(request)
        return login_response()

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ValueError, ValeriaError)):
            await client.create_task_run("task-1", idempotency_key=key)

    assert requests == []


async def test_idempotency_key_is_required_before_any_network_request():
    requests = []

    def handler(request):
        requests.append(request)
        return login_response()

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TypeError):
            await client.create_task_run("task-1")

    assert requests == []


@pytest.mark.parametrize("payload", [{"id": "run-1"}, [{"status": "queued"}], "secret"])
async def test_malformed_task_catalog_fails_with_a_safe_error(payload):
    def handler(request):
        if request.url.path.endswith("/auth/login"):
            return login_response()
        return httpx.Response(200, json=payload)

    async with ValeriaClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValeriaError):
            await client.list_tasks()
