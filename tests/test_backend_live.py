"""Opt-in tests against real CLIs and a local, deterministic model provider."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opencode_web.consultations import final_answer
from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker
from opencode_web.opencode_client import OpenCodeManager
from opencode_web.task_results import task_final_answer


@pytest.mark.parametrize("api_version", ["v1", "v2"])
async def test_real_backend_prompt_history_events_and_questions(
    settings, workspace, tmp_path, monkeypatch, api_version
):
    binary = os.getenv(f"OPENCODE_{api_version.upper()}_TEST_BIN")
    if not binary:
        pytest.skip(f"Set OPENCODE_{api_version.upper()}_TEST_BIN to run a real server")
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "Local test answer"}, None),
                ({}, "stop"),
            ]:
                chunk = {
                    "id": "chat-test",
                    "object": "chat.completion.chunk",
                    "model": "test",
                    "created": 1,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = {
        "model": "local/test",
        "small_model": "local/test",
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": f"http://127.0.0.1:{provider.server_port}/v1",
                    "apiKey": "test-only",
                },
                "models": {
                    "test": {"name": "Local test", "limit": {"context": 32000, "output": 1000}}
                },
            }
        },
    }
    if api_version == "v2" and os.getenv("OPENCODE_TEST_DCP") == "1":
        config["plugins"] = [str(Path(__file__).parents[1] / "mcp/opencode-dcp-local")]
    (config_dir / "opencode.jsonc").write_text(json.dumps(config))
    if api_version == "v1":
        monkeypatch.setenv("OPENCODE_CONFIG", str(config_dir / "opencode.jsonc"))
        for variable, name in (
            ("XDG_DATA_HOME", "data"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_CONFIG_HOME", "global-config"),
        ):
            monkeypatch.setenv(variable, str(tmp_path / name))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_settings = replace(
        settings,
        opencode_bin=Path(binary),
        opencode_api_version=api_version,
        opencode_v2_config_dir=config_dir,
        opencode_v2_state_root=tmp_path / "v2-data",
        opencode_port_start=port,
        opencode_port_end=port,
        opencode_start_timeout=30,
    )
    events = []

    async def receive(event):
        events.append(event)

    manager = OpenCodeManager(config_settings, receive)
    try:
        async with asyncio.timeout(90):
            client = await manager.ensure(str(workspace))
            assert (await client.health())["healthy"] is True
            assert Path((await client.path())["directory"]).resolve() == workspace
            # Server health can precede the initial location's agent catalog.
            for _ in range(50):
                agents = await client.agents()
                if any(agent["name"] == "build" for agent in agents):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail(f"No build agent after location startup: {agents!r}")
            assert any(provider["id"] == "local" for provider in (await client.providers())["all"])
            assert isinstance(await client.mcp_statuses(), dict)
            session = await client.create_session("Live probe")
            session_id = session["id"]
            await client.send_prompt_async(
                session_id,
                message_id="msg_live_probe",
                content="Reply with the test answer",
                agent="build",
                provider_id="local",
                model_id="test",
                model_variant=None,
                system="Give a concise answer.",
            )
            for _ in range(100):
                messages = await client.messages(session_id)
                answer = final_answer(messages, "msg_live_probe")
                if answer:
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail(f"No completed response: {messages!r}")
            assert answer == "Local test answer"
            assert task_final_answer(messages, "msg_live_probe")["text"] == answer
            assert requests
            assert events
            if api_version == "v2":
                # Retrying the same durable input ID must not execute it again.
                before = len(requests)
                await client.send_prompt_async(
                    session_id,
                    message_id="msg_live_probe",
                    content="Reply with the test answer",
                    agent="build",
                    provider_id="local",
                    model_id="test",
                    model_variant=None,
                    system="Give a concise answer.",
                )
                await asyncio.sleep(0.3)
                assert len(requests) == before
            assert not any(
                QueueStatus.busy(value) for value in (await client.session_statuses()).values()
            )
            if api_version == "v2":
                # Create an actual native form and answer through the shared contract.
                native = manager.instances()[0].client
                await native._request(
                    "POST",
                    f"/api/session/{session_id}/form",
                    json={
                        "title": "Choose",
                        "fields": [
                            {
                                "key": "pick",
                                "type": "string",
                                "title": "Select one",
                                "options": [{"value": "a", "label": "A (Recommended)"}],
                                "custom": False,
                            }
                        ],
                    },
                )
                question = (await client.questions())[0]
                assert question["sessionID"] == session_id
                assert await client.respond_question(question["id"], [["A (Recommended)"]])
                assert await client.questions() == []
            assert await client.abort(session_id)
            assert await client.delete_session(session_id)
            if api_version == "v2" and os.getenv("OPENCODE_TEST_DCP") == "1":
                native = manager.instances()[0].client
                plugins = await native._request("GET", "/api/plugin", params=native._location())
                plugin = next(item for item in plugins["data"] if item.get("id") == "opencode-dcp")
                assert plugin["state"]["status"] == "active", plugin
                status = await native._request(
                    "POST",
                    "/api/rpc/dcp/status",
                    params=native._location(),
                    json={"input": {}},
                )
                assert status["output"]["enabled"] is True, status

            # Run actual gateway workflows through the same adapter, including
            # the plan/build phase boundary and consultation system instructions.
            database = Database(config_settings.database_path, config_settings.session_secret)
            database.initialize()
            database.activate_backend(api_version)
            try:
                user = database.create_user(
                    "live-worker", "long-enough-password", "admin", str(workspace)
                )
                task = database.create_task(
                    user["id"],
                    "Live task",
                    "",
                    "Plan and answer",
                    "build",
                    "local",
                    "test",
                    None,
                    True,
                    plan_first=True,
                )
                task_job = database.create_task_job(
                    task=task,
                    user=user,
                    actor_user_id=user["id"],
                    session_id=None,
                    user_input="",
                    max_queued=10,
                )
                dispatcher = QueueDispatcher(database, manager, EventBroker(), 1)
                claimed = database.claim_next_job(1)
                assert claimed["id"] == task_job["id"]
                await dispatcher._execute(claimed)
                completed = database.get_job(task_job["id"])
                assert completed["status"] == "completed", completed
                assert completed["phase2_message_id"]
                assert database.get_task_result(task_job["id"])["text"] == "Local test answer"
                consultation = database.create_chat(
                    user["id"],
                    None,
                    "Consultation",
                    str(workspace),
                    consultation=True,
                )
                database.create_job(
                    chat=consultation,
                    effective_user_id=user["id"],
                    actor_user_id=user["id"],
                    content="Explain",
                    agent="build",
                    provider_id="local",
                    model_id="test",
                    model_variant=None,
                )
                await dispatcher._execute(database.claim_next_job(1))
                consultation_result = database.consultation_jobs(consultation["id"])[0]
                assert consultation_result["status"] == "completed", consultation_result
                assert consultation_result["answer"] == "Local test answer"
            finally:
                database.close()
    finally:
        await manager.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)


class QueueStatus:
    @staticmethod
    def busy(value):
        return isinstance(value, dict) and value.get("type") not in {"idle", "failed", "aborted"}
