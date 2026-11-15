from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from opencode_web.app import safe_opencode_event
from opencode_web.backends import OpenCodeBackend, OpenCodeError, create_backend
from opencode_web.backends.contracts import OpenCodeProtocolError, OpenCodeSubmissionUncertain
from opencode_web.backends.v2 import OpenCodeV2Backend
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.task_results import task_final_answer


@asynccontextmanager
async def backend(handler):
    client = OpenCodeV2Backend("http://opencode.test", "ignored", "password", workspace="/work")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        auth=("opencode", "password"),
        transport=httpx.MockTransport(handler),
    )
    try:
        yield client
    finally:
        await client.close()


def session(identifier="ses_one", **extra):
    return {"id": identifier, "location": {"directory": "/work"}, "time": {}, **extra}


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_factory_returns_complete_backend_contract(version):
    client = create_backend("http://test", "", "", api_version=version)
    try:
        assert isinstance(client, OpenCodeBackend)
    finally:
        await client.close()


async def test_health_checks_major_version_and_uses_basic_auth():
    def handler(request):
        assert request.url.path == "/api/info"
        assert request.headers["authorization"] == "Basic b3BlbmNvZGU6cGFzc3dvcmQ="
        return httpx.Response(200, json={"version": "2.3.0"})

    async with backend(handler) as client:
        assert (await client.health())["healthy"] is True
    async with backend(lambda request: httpx.Response(200, json={"version": "1.18.25"})) as client:
        with pytest.raises(OpenCodeProtocolError, match="2.x"):
            await client.health()


@pytest.mark.parametrize("unsent", [False, True])
async def test_creation_recovers_atomic_metadata_marker_and_only_retries_unsent_post(
    unsent, monkeypatch
):
    created = None
    posts = 0
    states = []

    async def immediate_retry(delay, cancel_event=None):
        pass

    monkeypatch.setattr("opencode_web.backends.creation._wait_for_retry", immediate_retry)

    def handler(request):
        nonlocal created, posts
        assert request.url.path == "/api/session"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [created] if created else [], "cursor": {}})
        body = json.loads(request.content)
        assert body["metadata"] == {"shodanCreationKey": "creation-one"}
        assert states[-1] is True
        posts += 1
        if posts == 1 and unsent:
            raise httpx.ConnectError("request not sent", request=request)
        created = session(metadata=body["metadata"])
        if posts == 1:
            raise httpx.ReadError("receipt lost", request=request)
        return httpx.Response(200, json={"data": created})

    def mark(started):
        states.append(started)
        return True

    async with backend(handler) as client:
        restored = await client.create_session(creation_key="creation-one", creation_state=mark)
    assert restored["id"] == "ses_one"
    assert restored["metadata"]["shodanCreationKey"] == "creation-one"
    assert posts == (2 if unsent else 1)
    assert states == ([True, False, True] if unsent else [True])


async def test_sessions_paginate_and_enforce_workspace():
    calls = []

    def handler(request):
        calls.append(request)
        if "cursor" not in request.url.params:
            assert request.url.params["directory"] == "/work"
            return httpx.Response(200, json={"data": [session()], "cursor": {"next": "page2"}})
        assert "order" not in request.url.params
        return httpx.Response(200, json={"data": [session("ses_two")], "cursor": {"next": None}})

    async with backend(handler) as client:
        assert [item["id"] for item in await client.sessions()] == ["ses_one", "ses_two"]
    assert len(calls) == 2
    async with backend(
        lambda request: httpx.Response(
            200,
            json={
                "data": session(location={"directory": "/other"}),
            },
        )
    ) as client:
        with pytest.raises(OpenCodeProtocolError, match="another workspace"):
            await client.session("ses_one")


async def test_repeated_cursor_does_not_loop_forever():
    async with backend(
        lambda request: httpx.Response(
            200,
            json={
                "data": [],
                "cursor": {"next": "same"},
            },
        )
    ) as client:
        with pytest.raises(OpenCodeProtocolError, match="pagination"):
            await client.sessions()


async def test_messages_project_compaction_final_answer_and_queued_admission():
    def handler(request):
        if request.url.path.endswith("/inbox"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "user",
                            "id": "msg_queued",
                            "payload": {"text": "Next"},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"type": "user", "id": "msg_user", "text": "Do it"},
                    {
                        "type": "compaction",
                        "id": "msg_compact",
                        "status": "completed",
                        "summary": "Context",
                    },
                    {
                        "type": "assistant",
                        "id": "msg_answer",
                        "finish": "stop",
                        "time": {"completed": 20},
                        "content": [
                            {"type": "reasoning", "text": "Think"},
                            {"type": "text", "text": "Done"},
                        ],
                    },
                ],
                "cursor": {},
            },
        )

    async with backend(handler) as client:
        messages = await client.messages("ses_one")
        assert messages[1]["info"]["summary"] is True
        assert messages[2]["info"]["parentID"] == "msg_user"
        assert messages[-1]["info"]["id"] == "msg_queued"
        assert task_final_answer(messages[:-1], "msg_user") == {
            "message_id": "msg_answer",
            "text": "Done",
        }
        assert QueueDispatcher._message_result(messages, "msg_user") == (True, "completed")


async def test_prompt_switches_agent_model_and_instructions_before_durable_admission():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"data": {"id": "msg_one", "sessionID": "ses_one"}})
        return httpx.Response(204)

    async with backend(handler) as client:
        await client.send_prompt_async(
            "ses_one",
            message_id="msg_one",
            content="Hello",
            agent="plan",
            provider_id="local",
            model_id="test",
            model_variant="high",
            system="Read only",
        )
    assert calls == [
        ("POST", "/api/session/ses_one/agent", {"agent": "plan"}),
        (
            "POST",
            "/api/session/ses_one/model",
            {"model": {"providerID": "local", "id": "test", "variant": "high"}},
        ),
        (
            "PUT",
            "/api/experimental/session/ses_one/instructions/entries/valeria.system",
            {"value": "Read only"},
        ),
        (
            "POST",
            "/api/session/ses_one/prompt",
            {"id": "msg_one", "text": "Hello", "delivery": "queue", "resume": True},
        ),
    ]


@pytest.mark.parametrize("accepted", [True, False])
async def test_lost_prompt_receipt_reconciles_inbox_without_resubmitting(accepted):
    prompts = []

    def handler(request):
        if request.url.path.endswith("/agent") or request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/prompt"):
            prompts.append(request)
            raise httpx.ReadTimeout("lost response", request=request)
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"data": [], "cursor": {}})
        assert request.url.path.endswith("/inbox")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "user",
                        "id": "msg_one",
                        "payload": {"text": "Hello"},
                    }
                ]
                if accepted
                else []
            },
        )

    async with backend(handler) as client:
        prompt = client.send_prompt_async(
            "ses_one",
            message_id="msg_one",
            content="Hello",
            agent="build",
            provider_id=None,
            model_id=None,
            model_variant=None,
        )
        if accepted:
            await prompt
        else:
            with pytest.raises(OpenCodeSubmissionUncertain):
                await prompt
    assert len(prompts) == 1


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 405, 413, 415, 422])
async def test_definite_prompt_rejection_does_not_wait_for_admission(status_code):
    def handler(request):
        if request.url.path.endswith("/agent") or request.method == "DELETE":
            return httpx.Response(204)
        assert request.url.path.endswith("/prompt")
        return httpx.Response(status_code, json={"message": "rejected"})

    async with backend(handler) as client:
        with pytest.raises(OpenCodeError) as rejected:
            await client.send_prompt_async(
                "ses_one",
                message_id="msg_one",
                content="Hello",
                agent="build",
                provider_id=None,
                model_id=None,
                model_variant=None,
            )
        assert rejected.value.status_code == status_code
        assert not isinstance(rejected.value, OpenCodeSubmissionUncertain)


async def test_inbox_to_execution_transition_overrides_previous_terminal_outcome():
    active_reads = 0

    def handler(request):
        nonlocal active_reads
        if request.url.path == "/api/session/active":
            active_reads += 1
            return httpx.Response(
                200,
                json={"data": {} if active_reads == 1 else {"ses_one": {"type": "running"}}},
            )
        if request.url.path.endswith("/inbox"):
            return httpx.Response(200, json={"data": []})
        assert request.url.path == "/api/session"
        return httpx.Response(200, json={"data": [session(outcome="failed")], "cursor": {}})

    async with backend(handler) as client:
        assert await client.session_statuses() == {"ses_one": {"type": "busy"}}
    assert active_reads == 2


async def test_abort_cancels_pending_inputs_and_accepts_empty_http_replies():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "msg_pending"}]})
        if request.url.path.endswith("/interrupt"):
            assert json.loads(request.content) == {"resume": False}
            return httpx.Response(200, json={"interrupted": True})
        return httpx.Response(204)

    async with backend(handler) as client:
        assert await client.abort("ses_one") is True
    assert calls == [
        ("GET", "/api/session/ses_one/inbox"),
        ("DELETE", "/api/session/ses_one/inbox/msg_pending"),
        ("POST", "/api/session/ses_one/interrupt"),
    ]


async def test_form_option_labels_are_translated_to_native_values():
    replies = []
    form = {
        "id": "form_one",
        "sessionID": "ses_one",
        "title": "Choose",
        "fields": [
            {
                "key": "choice",
                "type": "string",
                "options": [{"label": "A (Recommended)", "value": "a"}],
                "custom": False,
            },
            {"key": "many", "type": "multiselect", "options": [{"label": "B", "value": "b"}]},
            {"key": "enabled", "type": "boolean"},
            {"key": "count", "type": "integer"},
        ],
    }

    def handler(request):
        if request.method == "GET":
            assert request.url.params["location[directory]"] == "/work"
            return httpx.Response(200, json={"data": [form]})
        replies.append(json.loads(request.content))
        return httpx.Response(204)

    async with backend(handler) as client:
        questions = await client.questions()
        assert questions[0]["questions"][0]["options"] == [{"label": "A (Recommended)"}]
        assert await client.respond_question(
            "form_one", [["A (Recommended)"], ["B"], ["true"], ["3"]]
        )
    assert replies == [{"answer": {"choice": "a", "many": ["b"], "enabled": True, "count": 3}}]


async def test_permission_reply_uses_native_endpoint_and_204_is_success():
    def handler(request):
        assert request.url.path == "/api/session/ses_one/permission/perm_one/reply"
        assert json.loads(request.content) == {"decision": "always"}
        return httpx.Response(204)

    async with backend(handler) as client:
        assert await client.respond_permission("ses_one", "perm_one", "once", True)


async def test_events_use_explicit_owners_and_filter_other_locations():
    events = [
        {
            "type": "form.created",
            "location": {"directory": "/work"},
            "data": {"form": {"sessionID": "ses_one"}},
        },
        {
            "type": "session.text.delta",
            "location": {"directory": "/other"},
            "data": {"sessionID": "ses_other"},
        },
        {"type": "custom.event", "data": {"unrelated": {"sessionID": "ses_one"}}},
    ]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    async with backend(lambda request: httpx.Response(200, text=body)) as client:
        normalized = [event async for event in client.global_events()]
    assert len(normalized) == 2
    assert safe_opencode_event({"event": normalized[0]})["session_id"] == "ses_one"
    assert safe_opencode_event({"event": normalized[1]})["session_id"] is None


async def test_malformed_json_is_a_backend_error():
    async with backend(
        lambda request: httpx.Response(200, text="<html>wrong server</html>")
    ) as client:
        with pytest.raises(OpenCodeError, match="invalid JSON"):
            await client.health()
