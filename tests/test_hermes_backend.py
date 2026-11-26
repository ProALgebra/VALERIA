from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from opencode_web.backends.contracts import OpenCodeSubmissionUncertain
from opencode_web.backends.factory import create_backend
from opencode_web.backends.hermes import (
    HermesBackend,
    MemoryHermesStateStore,
    _control_id,
)
from opencode_web.database import Database


def test_database_persists_hermes_bindings_and_session_meta(tmp_path: Path) -> None:
    database = Database(tmp_path / "hermes.sqlite", "x" * 32)
    database.initialize()
    try:
        binding = database.save_hermes_binding(
            message_id="msg_db",
            job_id="job_db",
            session_id="session_db",
            content="hello",
            phase="build",
            run_id="run_db",
            status="started",
        )
        assert binding["ordinal"] == 1
        assert database.get_hermes_binding_by_run("run_db")["message_id"] == "msg_db"
        database.update_hermes_binding("msg_db", status="completed", output="done")
        active = database.list_hermes_bindings(active_only=True)
        assert active == []
        database.save_hermes_session_meta(
            "session_db",
            workspace=str(tmp_path),
            permission=[],
        )
        meta = database.get_hermes_session_meta("session_db")
        assert meta is not None
        assert meta["workspace"] == str(tmp_path)
    finally:
        database.close()


def make_backend(
    workspace: Path,
    handler,
    store: MemoryHermesStateStore | None = None,
) -> tuple[HermesBackend, MemoryHermesStateStore]:
    state = store or MemoryHermesStateStore()
    backend = create_backend(
        "http://hermes.test",
        "",
        "",
        api_version="hermes",
        workspace=str(workspace),
        api_key="test-key",
        state_store=state,
    )
    assert isinstance(backend, HermesBackend)
    backend._client = httpx.AsyncClient(
        base_url="http://hermes.test",
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handler),
    )
    return backend, state


async def test_create_session_is_deterministic_and_returns_permission(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"error": "not found"})
        import json

        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={"object": "hermes.session", "session": {"id": body["id"]}},
        )

    backend, store = make_backend(tmp_path, handler)
    try:
        session = await backend.create_session(
            "Chat", permission=[], creation_key="job-1"
        )
    finally:
        await backend.close()

    assert session["id"].startswith("valeria-")
    assert session["directory"] == str(tmp_path.resolve())
    assert session["permission"] == []
    assert store.get_hermes_session_meta(session["id"]) is not None
    assert ("GET", f"/api/sessions/{session['id']}") in requests
    assert ("POST", "/api/sessions") in requests


async def test_send_prompt_binds_run_and_normalizes_final_output(tmp_path: Path) -> None:
    calls = {"runs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs" and request.method == "POST":
            calls["runs"] += 1
            assert request.headers["Idempotency-Key"] == "msg_1"
            return httpx.Response(202, json={"run_id": "run_1", "status": "started"})
        if request.url.path == "/v1/runs/run_1":
            return httpx.Response(
                200,
                json={
                    "object": "hermes.run",
                    "run_id": "run_1",
                    "status": "completed",
                    "session_id": "session_1",
                    "output": "готово",
                    "pending_controls": [],
                },
            )
        if request.url.path == "/api/sessions/session_1/messages":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "session_id": "session_1",
                    "data": [
                        {
                            "id": "h-user",
                            "role": "user",
                            "content": "сделай",
                            "timestamp": 1,
                        },
                        {
                            "id": "h-assistant",
                            "role": "assistant",
                            "content": "готово",
                            "finish_reason": "stop",
                            "timestamp": 2,
                        },
                    ],
                    "pagination": {},
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    backend, store = make_backend(tmp_path, handler)
    try:
        await backend.send_prompt_async(
            "session_1",
            message_id="msg_1",
            content="сделай",
            agent="build",
            provider_id="p",
            model_id="m",
            model_variant=None,
            system="system",
            phase="build",
        )
        binding = store.get_hermes_binding("msg_1")
        assert binding is not None
        assert binding["run_id"] == "run_1"

        statuses = await backend.session_statuses()
        assert statuses == {"session_1": "idle"}
        messages = await backend.messages("session_1")
        assert messages[0]["info"]["id"] == "msg_1"
        assert messages[1]["info"]["parentID"] == "msg_1"
        assert messages[1]["info"]["finish"] == "stop"

        # Exact retry must not create a second Hermes run.
        await backend.send_prompt_async(
            "session_1",
            message_id="msg_1",
            content="сделай",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        assert calls["runs"] == 1
    finally:
        await backend.close()


async def test_missing_transcript_user_is_reconciled_from_binding(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run_2", "status": "queued"})
        if request.url.path == "/api/sessions/session_2/messages":
            return httpx.Response(
                200,
                json={"object": "list", "session_id": "session_2", "data": []},
            )
        return httpx.Response(404, json={"error": "not found"})

    backend, _ = make_backend(tmp_path, handler)
    try:
        await backend.send_prompt_async(
            "session_2",
            message_id="msg_2",
            content="текст",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        messages = await backend.messages("session_2")
    finally:
        await backend.close()

    assert messages[0]["info"]["id"] == "msg_2"
    assert messages[0]["parts"][0]["text"] == "текст"


async def test_approval_and_clarify_controls(tmp_path: Path) -> None:
    answered: list[dict[str, Any]] = []
    run_status = {
        "object": "hermes.run",
        "run_id": "run_3",
        "status": "waiting_for_approval",
        "session_id": "session_3",
        "execution_generation": 7,
        "pending_controls": [
            {
                "kind": "approval",
                "request_id": "req-a",
                "description": "run tests",
                "command": "pytest",
                "choices": ["once", "deny"],
            },
            {
                "kind": "clarify",
                "request_id": "req-q",
                "question": "Какой файл?",
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run_3", "status": "queued"})
        if request.url.path == "/v1/runs/run_3":
            return httpx.Response(200, json=run_status)
        if request.url.path == "/v1/runs/run_3/approval":
            answered.append({"kind": "approval", "body": request.content})
            return httpx.Response(200, json={"resolved": 1})
        if request.url.path == "/v1/runs/run_3/clarify":
            answered.append({"kind": "clarify", "body": request.content})
            return httpx.Response(200, json={"resolved": 1})
        return httpx.Response(404, json={"error": "not found"})

    backend, _ = make_backend(tmp_path, handler)
    try:
        await backend.send_prompt_async(
            "session_3",
            message_id="msg_3",
            content="вопрос",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        permissions = await backend.permissions()
        questions = await backend.questions()
        assert permissions[0]["id"] == _control_id("run_3", "req-a", 7)
        assert questions[0]["id"] == _control_id("run_3", "req-q", 7)
        assert await backend.respond_permission(
            "session_3", permissions[0]["id"], "once", False
        )
        assert await backend.respond_question(questions[0]["id"], [["answer"]])
    finally:
        await backend.close()

    assert [item["kind"] for item in answered] == ["approval", "clarify"]


async def test_unknown_run_is_resolved_with_admission_identity(tmp_path: Path) -> None:
    calls = {"get": 0, "resolve": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run_u", "status": "started"})
        if request.url.path == "/v1/runs/run_u":
            calls["get"] += 1
            if calls["get"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "run_id": "run_u",
                        "status": "unknown",
                        "session_id": "session_u",
                        "admission_id": "adm_u",
                        "execution_generation": 9,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "run_id": "run_u",
                    "status": "completed",
                    "session_id": "session_u",
                    "output": "ok",
                },
            )
        if request.url.path == "/v1/runs/run_u/resolve-unknown":
            calls["resolve"] += 1
            return httpx.Response(200, json={"run_id": "run_u"})
        return httpx.Response(404, json={"error": "not found"})

    backend, _ = make_backend(tmp_path, handler)
    try:
        await backend.send_prompt_async(
            "session_u",
            message_id="msg_u",
            content="work",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        statuses = await backend.session_statuses()
    finally:
        await backend.close()

    assert calls["resolve"] == 1
    assert statuses == {"session_u": "idle"}


async def test_uncertain_submission_replays_idempotently(tmp_path: Path) -> None:
    calls = {"runs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            calls["runs"] += 1
            if calls["runs"] == 1:
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(
                202,
                json={"run_id": "run_4", "status": "started"},
                headers={"Idempotency-Replayed": "true"},
            )
        if request.url.path == "/v1/runs/run_4":
            return httpx.Response(
                200,
                json={
                    "object": "hermes.run",
                    "run_id": "run_4",
                    "status": "running",
                    "session_id": "session_4",
                    "pending_controls": [],
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    backend, _ = make_backend(tmp_path, handler)
    try:
        with pytest.raises(OpenCodeSubmissionUncertain):
            await backend.send_prompt_async(
                "session_4",
                message_id="msg_4",
                content="retry",
                agent="build",
                provider_id=None,
                model_id=None,
                model_variant=None,
            )
        status = await backend.session_statuses()
        assert status == {"session_4": "busy"}
        binding = backend._store.get_hermes_binding("msg_4")
        assert binding is not None and binding["run_id"] == "run_4"
        assert calls["runs"] == 2
    finally:
        await backend.close()
