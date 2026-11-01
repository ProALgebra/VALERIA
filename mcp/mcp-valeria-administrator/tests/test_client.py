import asyncio
import json

import httpx
import pytest

from admin_client import AdminClient, AdminError
from admin_config import Settings


def settings():
    return Settings(
        url="https://valeria.example/team/",
        login="administrator",
        password="private-administrator-password",
    )


def login_response(generation=1, role="admin"):
    return httpx.Response(
        200,
        headers={"set-cookie": f"valeria_session=session-{generation}; Path=/; HttpOnly"},
        json={"user": {"id": "admin-1", "role": role}, "csrf_token": f"csrf-{generation}"},
    )


async def test_cookie_csrf_base_path_query_and_no_content_workflow():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content) == {
                "login": "administrator", "password": settings().password,
            }
            return login_response()
        assert request.headers["cookie"] == "valeria_session=session-1"
        if request.method == "POST":
            assert request.headers["x-csrf-token"] == "csrf-1"
            assert json.loads(request.content) == {"title": "New task"}
            return httpx.Response(201, json={"id": "task-1", "title": "New task"})
        if request.method == "DELETE":
            assert request.headers["x-csrf-token"] == "csrf-1"
            return httpx.Response(204)
        assert dict(request.url.params) == {"day": "2026-11-01", "timezone": "Europe/Belgrade"}
        return httpx.Response(200, json={"day": "2026-11-01"})

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        assert await client.request(
            "POST", "api/admin/tasks", expected_status=201, body={"title": "New task"},
        ) == {"id": "task-1", "title": "New task"}
        assert await client.request(
            "GET", "api/admin/statistics",
            params={"day": "2026-11-01", "timezone": "Europe/Belgrade"},
        ) == {"day": "2026-11-01"}
        assert await client.request(
            "DELETE", "api/admin/tasks/task-1", expected_status=204,
        ) == {"ok": True}

    assert [request.url.path for request in requests] == [
        "/team/api/auth/login", "/team/api/admin/tasks", "/team/api/admin/statistics",
        "/team/api/admin/tasks/task-1",
    ]


async def test_concurrent_first_requests_share_one_admin_login():
    started = asyncio.Event()
    finish_login = asyncio.Event()
    login_count = 0

    async def handler(request):
        nonlocal login_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            started.set()
            await finish_login.wait()
            return login_response()
        return httpx.Response(200, json=[])

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        calls = [asyncio.create_task(client.request("GET", "api/admin/users")) for _ in range(5)]
        await asyncio.wait_for(started.wait(), timeout=2)
        finish_login.set()
        assert await asyncio.gather(*calls) == [[]] * 5
    assert login_count == 1


@pytest.mark.parametrize("role", ["user", None, "ADMIN"])
async def test_non_administrator_account_cannot_issue_admin_requests(role):
    requests = []

    def handler(request):
        requests.append(request)
        return login_response(role=role)

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request("POST", "api/admin/users", body={"login": "new-user"})

    assert [request.url.path for request in requests] == ["/team/api/auth/login"]
    assert caught.value.status_code == 403
    assert settings().password not in str(caught.value)


async def test_expired_session_replays_once_with_same_mutation_and_key():
    login_count = 0
    submissions = []

    def handler(request):
        nonlocal login_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            return login_response(login_count)
        submissions.append(request)
        if len(submissions) == 1:
            return httpx.Response(401, text="private-expired-session-response")
        assert request.headers["cookie"] == "valeria_session=session-2"
        assert request.headers["x-csrf-token"] == "csrf-2"
        return httpx.Response(202, json={"id": "run-1", "status": "queued"})

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        assert await client.request(
            "POST", "api/tasks/task-1/runs", expected_status=202,
            body={"user_input": "November"}, idempotency_key="stable-launch-key",
        ) == {"id": "run-1", "status": "queued"}

    assert login_count == 2
    assert len(submissions) == 2
    assert [request.headers["idempotency-key"] for request in submissions] == [
        "stable-launch-key", "stable-launch-key",
    ]
    assert submissions[0].content == submissions[1].content


async def test_repeated_unauthorized_response_stops_after_one_relogin():
    login_count = 0
    call_count = 0

    def handler(request):
        nonlocal login_count, call_count
        if request.url.path.endswith("/auth/login"):
            login_count += 1
            return login_response(login_count)
        call_count += 1
        return httpx.Response(401, text="private-response-body")

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request("GET", "api/admin/users")
    assert login_count == call_count == 2
    assert caught.value.status_code == 401
    assert "private-response-body" not in str(caught.value)


@pytest.mark.parametrize("key", [None, "recoverable-key"])
async def test_ambiguous_mutation_is_not_retried(key):
    submissions = []

    def handler(request):
        if request.url.path.endswith("/auth/login"):
            return login_response()
        submissions.append(request)
        raise httpx.ReadTimeout("private-transport-detail", request=request)

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request(
                "POST", "api/admin/users", expected_status=201,
                body={"login": "new-user", "password": "private-new-password"},
                idempotency_key=key,
            )

    assert len(submissions) == 1
    assert caught.value.status_code is None
    assert caught.value.idempotency_key == key
    expected = "reuse the same Idempotency-Key" if key else "inspect state before retrying"
    assert expected in str(caught.value)
    assert "private-transport-detail" not in str(caught.value)
    assert "private-new-password" not in str(caught.value)


@pytest.mark.parametrize("status", [403, 404, 409, 422, 429, 500, 503])
async def test_api_errors_keep_status_without_replay_or_response_body(status):
    call_count = 0

    def handler(request):
        nonlocal call_count
        if request.url.path.endswith("/auth/login"):
            return login_response()
        call_count += 1
        return httpx.Response(status, json={"detail": "private-response-body"})

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request("PATCH", "api/admin/users/user-1", body={"status": "disabled"})

    assert call_count == 1
    assert caught.value.status_code == status
    assert "private-response-body" not in str(caught.value)
    assert settings().password not in str(caught.value)
    if status >= 500:
        assert "inspect state before retrying" in str(caught.value)


async def test_failed_login_never_sends_an_admin_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, text="private-auth-response")

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request("GET", "api/admin/audit")
    assert len(requests) == 1
    assert caught.value.status_code == 401
    assert "private-auth-response" not in str(caught.value)
    assert settings().password not in str(caught.value)


@pytest.mark.parametrize("path", [
    "", "/api/admin/users", "https://elsewhere.example/api/admin/users",
    "api/../auth/logout", "api/admin/users?password=secret", "api//admin/users",
])
async def test_unsafe_api_path_is_rejected_before_authentication(path):
    requests = []

    def handler(request):
        requests.append(request)
        return login_response()

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await client.request("GET", path)
    assert requests == []


@pytest.mark.parametrize("key", ["", " ", "bad\nheader", "ключ", "a" * 201])
async def test_bad_submission_key_is_rejected_before_authentication(key):
    requests = []

    def handler(request):
        requests.append(request)
        return login_response()

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await client.request("POST", "api/tasks/task-1/runs", idempotency_key=key)
    assert requests == []


@pytest.mark.parametrize("payload", [None, "private-json-value", 123])
async def test_malformed_json_response_is_a_safe_error(payload):
    def handler(request):
        if request.url.path.endswith("/auth/login"):
            return login_response()
        return httpx.Response(200, json=payload)

    async with AdminClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdminError) as caught:
            await client.request("GET", "api/admin/users")
    assert caught.value.status_code == 200
    assert "private-json-value" not in str(caught.value)
