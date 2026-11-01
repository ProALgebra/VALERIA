"""User session isolation and durable requests through the real Valeria API."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from opencode_web.app import create_app
from opencode_web.config import Settings as ApplicationSettings

from valeria_telegram.valeria_client import ValeriaClient, ValeriaError


@pytest.fixture
def client_settings():
    return SimpleNamespace(
        valeria_url="http://test",
        ca_bundle=None,
        request_timeout_seconds=2,
    )


async def test_login_and_restored_session_send_private_cookies_and_csrf(client_settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"set-cookie": "ocw_session=session-A; HttpOnly; Path=/"},
                json={"csrf_token": "csrf-A", "user": {"id": "user-A", "login": "alice"}},
            )
        return httpx.Response(204)

    transport = httpx.MockTransport(handle)
    async with ValeriaClient(client_settings, transport=transport) as client:
        session = await client.login("alice", "private-password")
        assert session == {
            "session_token": "session-A",
            "csrf_token": "csrf-A",
            "user": {"id": "user-A", "login": "alice"},
        }
        assert "private-password" not in str(vars(client))
    async with ValeriaClient(
        client_settings,
        session_token=session["session_token"],
        csrf_token=session["csrf_token"],
        transport=transport,
    ) as restored:
        assert await restored.request("POST", "/api/auth/logout", expected_status=204) == {
            "ok": True,
        }
    assert requests[1].headers["cookie"] == "ocw_session=session-A"
    assert requests[1].headers["x-csrf-token"] == "csrf-A"


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 503])
async def test_http_failures_are_safe_and_never_replayed(client_settings, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "SECRET-PASSWORD-OR-PROMPT"})

    async with ValeriaClient(
        client_settings,
        session_token="one-session",
        csrf_token="one-csrf",
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.request(
                "POST",
                "api/tasks/task/runs",
                body={"user_input": "private-prompt"},
                expected_status=202,
                idempotency_key="original-launch-key",
            )
    assert len(requests) == 1
    assert caught.value.status_code == status
    assert caught.value.idempotency_key == "original-launch-key"
    assert caught.value.acceptance_unknown is (status >= 500)
    assert "SECRET" not in str(caught.value)
    assert "private-prompt" not in str(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


@pytest.mark.parametrize("method,uncertain", [("POST", True), ("GET", False)])
async def test_network_failure_preserves_key_and_never_replays(client_settings, method, uncertain):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("SECRET PASSWORD", request=request)

    async with ValeriaClient(
        client_settings,
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.request(method, "api/tasks", idempotency_key="same-key")
    assert len(requests) == 1
    assert caught.value.idempotency_key == "same-key"
    assert caught.value.status_code is None
    assert caught.value.acceptance_unknown is uncertain
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("payload", ["not-json", "null", "42", '"text"'])
async def test_invalid_success_response_is_uncertain_for_submission(client_settings, payload):
    async with ValeriaClient(
        client_settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(202, text=payload)),
    ) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.request(
                "POST",
                "api/tasks/task/runs",
                expected_status=202,
                idempotency_key="save-me",
            )
    assert caught.value.idempotency_key == "save-me"
    assert caught.value.acceptance_unknown


async def test_redirects_do_not_forward_session_or_replay_submission(client_settings):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.invalid/api/tasks"})

    async with ValeriaClient(
        client_settings,
        session_token="SECRET-SESSION",
        csrf_token="SECRET-CSRF",
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.request("POST", "api/tasks/task/runs", expected_status=202)
    assert len(requests) == 1
    assert requests[0].url.host == "test"
    assert caught.value.acceptance_unknown


@pytest.mark.parametrize(
    "path",
    [
        "https://attacker.invalid/api/tasks",
        "//attacker.invalid/api/tasks",
        "api/../auth",
        "api/%2e%2e/auth",
        "api/%252e%252e/auth",
        "api/tasks\r\nx:secret",
        "api/tasks?secret=1",
        "api/tasks#other",
        "api\\tasks",
        "admin/users",
        "api/tasks/%2felsewhere",
        "api/tasks/%252felsewhere",
        "api/tasks/\u0085secret",
    ],
)
async def test_unsafe_paths_are_rejected_before_http(client_settings, path):
    requests = []
    async with ValeriaClient(
        client_settings,
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    ) as client:
        with pytest.raises(ValeriaError):
            await client.request("GET", path)
    assert not requests


@pytest.mark.parametrize("session,csrf", [("bad\r\ncookie", "csrf"), ("token", "bad\nheader")])
def test_invalid_restored_tokens_are_rejected(client_settings, session, csrf):
    with pytest.raises(ValeriaError):
        ValeriaClient(client_settings, session_token=session, csrf_token=csrf)


@pytest.mark.parametrize(
    "headers,payload",
    [
        ({}, {"csrf_token": "csrf", "user": {"id": "one", "login": "alice"}}),
        ({"set-cookie": "ocw_session=session; Path=/"}, {"csrf_token": "csrf", "user": {}}),
        ({"set-cookie": "ocw_session=session; Path=/"}, {"csrf_token": "bad\ncsrf", "user": {}}),
    ],
)
async def test_invalid_login_response_clears_session(client_settings, headers, payload):
    async with ValeriaClient(
        client_settings,
        session_token="earlier-session",
        csrf_token="earlier-csrf",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers=headers,
                json=payload,
            )
        ),
    ) as client:
        with pytest.raises(ValeriaError):
            await client.login("alice", "password")
        assert client.session_token is None
        assert client.csrf_token is None
        assert not list(client.client.cookies.items())


@pytest.fixture
async def user_api(tmp_path: Path, monkeypatch, client_settings):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    password_file = tmp_path / "admin-password"
    password_file.write_text("correct-horse-battery-staple", encoding="utf-8")
    settings = ApplicationSettings(
        host="127.0.0.1",
        port=8080,
        base_url="http://test",
        database_path=tmp_path / "test.db",
        cookie_secure=False,
        max_active_sessions=1,
        max_active_sessions_hard_limit=5,
        max_active_sessions_per_user=0,
        max_active_sessions_per_user_hard_limit=5,
        max_queued_messages=100,
        allowed_workspace_roots=(workspace,),
        allow_shared_workspaces=False,
        opencode_bin=Path("/usr/bin/opencode"),
        opencode_host="127.0.0.1",
        opencode_port_start=45100,
        opencode_port_end=45120,
        opencode_username="opencode",
        opencode_password="test-opencode-password",
        default_agent="build",
        allowed_agents=("build", "plan"),
        default_model=None,
        allowed_models=("*",),
        opencode_start_timeout=2,
        session_secret="s" * 32,
        auth_session_ttl_hours=24,
        bootstrap_admin_login="admin",
        bootstrap_admin_password_file=password_file,
        log_level="INFO",
    )
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        ensure = AsyncMock(side_effect=AssertionError("Queued requests must not start OpenCode"))
        monkeypatch.setattr(app.state.manager, "ensure", ensure)
        db = app.state.database
        users = {}
        for login, tasks_only in (("alice", False), ("bob", True)):
            folder = workspace / login
            folder.mkdir()
            users[login] = db.create_user(
                login,
                "a-strong-password",
                "user",
                str(folder),
                tasks_only=tasks_only,
            )
        admin = db.get_user_by_login("admin")
        task = db.create_task(
            admin["id"],
            "Available task",
            "Public description",
            "PRIVATE ADMIN PROMPT",
            "build",
            "provider",
            "coder",
            None,
            True,
        )
        db.set_consultation_model("provider/coder", admin["id"])
        yield SimpleNamespace(
            settings=client_settings,
            transport=httpx.ASGITransport(app=app),
            db=db,
            task=task,
            users=users,
        )
        ensure.assert_not_awaited()


async def test_real_api_login_isolation_durable_task_and_result(user_api):
    t = user_api
    async with (
        ValeriaClient(t.settings, transport=t.transport) as alice,
        ValeriaClient(t.settings, transport=t.transport) as bob,
    ):
        await alice.login("alice", "a-strong-password")
        await bob.login("bob", "a-strong-password")
        catalog = await alice.request("GET", "api/tasks")
        assert [task["id"] for task in catalog] == [t.task["id"]]
        assert "PRIVATE ADMIN PROMPT" not in str(catalog)
        path = f"api/tasks/{t.task['id']}/runs"
        run = await alice.request(
            "POST",
            path,
            body={"user_input": "Check the build"},
            expected_status=202,
            idempotency_key="telegram-update-42",
        )
        assert run["status"] == "queued"
        assert (
            await alice.request(
                "POST",
                path,
                body={"user_input": "Check the build"},
                expected_status=202,
                idempotency_key="telegram-update-42",
            )
            == run
        )
        assert len(t.db.list_jobs()) == 1
        job = t.db.get_job(run["id"])
        assert job["effective_user_id"] == t.users["alice"]["id"]
        assert job["actor_user_id"] == t.users["alice"]["id"]
        assert await bob.request("GET", "api/task-runs") == []
        with pytest.raises(ValeriaError) as caught:
            await bob.request("GET", f"api/task-runs/{run['id']}")
        assert caught.value.status_code == 404
        t.db.complete_task_run(run["id"], {"text": "Готово", "message_id": "answer"})
        assert await alice.request("GET", f"api/task-runs/{run['id']}/result") == {
            "run_id": run["id"],
            "status": "available",
            "text": "Готово",
        }
        with pytest.raises(ValeriaError) as caught:
            await bob.request("GET", f"api/task-runs/{run['id']}/result")
        assert caught.value.status_code == 404


async def test_real_api_consultations_tasks_only_and_pending_dialog(user_api):
    t = user_api
    async with (
        ValeriaClient(t.settings, transport=t.transport) as alice,
        ValeriaClient(t.settings, transport=t.transport) as bob,
    ):
        await alice.login("alice", "a-strong-password")
        bob_session = await bob.login("bob", "a-strong-password")
        assert bob_session["user"]["tasks_only"] is True
        consultation = await bob.request(
            "POST",
            "api/consultations",
            body={"message": "Explain the code"},
            expected_status=201,
            idempotency_key="consultation-42",
        )
        assert consultation["status"] == "queued"
        assert consultation["messages"][0]["text"] == "Explain the code"
        assert await bob.request("GET", f"api/consultations/{consultation['id']}/questions") == []
        with pytest.raises(ValeriaError) as caught:
            await alice.request("GET", f"api/consultations/{consultation['id']}")
        assert caught.value.status_code == 404
        with pytest.raises(ValeriaError) as caught:
            await bob.request("GET", "api/chats")
        assert caught.value.status_code == 403
        dialog = await alice.request(
            "POST",
            "api/chats",
            body={"message": "Investigate CI", "agent": "plan"},
            expected_status=201,
            idempotency_key="dialog-42",
        )
        chat_id = dialog["chat"]["id"]
        assert dialog["job"]["status"] == "queued"
        assert dialog["job"]["agent"] == "plan"
        history = await alice.request("GET", f"api/chats/{chat_id}/messages")
        assert history[0]["parts"][0]["text"] == "Investigate CI"
        assert await alice.request("GET", f"api/chats/{chat_id}/questions") == []
        assert await alice.request("GET", f"api/chats/{chat_id}/permissions") == []
        assert await alice.request(
            "DELETE",
            f"api/jobs/{dialog['job']['id']}",
            expected_status=204,
        ) == {"ok": True}


async def test_real_api_session_expiry_and_wrong_password_are_safe(user_api):
    t = user_api
    async with ValeriaClient(t.settings, transport=t.transport) as client:
        with pytest.raises(ValeriaError) as caught:
            await client.login("alice", "WRONG PRIVATE PASSWORD")
        assert caught.value.status_code == 401
        assert "Неверный логин" in str(caught.value)
        assert "PRIVATE" not in str(caught.value)
        session = await client.login("alice", "a-strong-password")
        t.db.delete_auth_session(session["session_token"])
        with pytest.raises(ValeriaError) as caught:
            await client.request("GET", "api/auth/me")
        assert caught.value.status_code == 401
        assert "Сессия истекла" in str(caught.value)
