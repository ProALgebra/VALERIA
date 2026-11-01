"""Verify the public tools through real MCP sessions and the stdio entrypoint."""

import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from config import Settings
from server import create_server

TOOL_NAMES = {
    "valeria_list_tasks", "valeria_create_task_run", "valeria_list_task_runs",
    "valeria_get_task_run", "valeria_get_task_result",
}


async def test_mcp_workflow_uses_only_configured_account():
    requests = []
    run = {"id": "run-1", "task_id": "task-1", "status": "queued", "phase": "build"}

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            assert json.loads(request.content) == {"login": "mcp-user", "password": "secret"}
            return httpx.Response(
                200, json={"csrf_token": "csrf"},
                headers={"set-cookie": "ocw_session=session; Path=/; HttpOnly"},
            )
        assert request.headers["cookie"] == "ocw_session=session"
        if path == "/api/tasks":
            payload = [{"id": "task-1", "title": "Проверка", "description": "Описание"}]
        elif path == "/api/tasks/task-1/runs":
            assert request.method == "POST"
            assert request.headers["x-csrf-token"] == "csrf"
            assert request.headers["idempotency-key"] == "request-1"
            assert json.loads(request.content) == {"user_input": "Дополнение"}
            return httpx.Response(202, json=run)
        elif path == "/api/task-runs":
            payload = [run]
        elif path == "/api/task-runs/run-1":
            run["status"] = "completed"
            payload = run
        elif path == "/api/task-runs/run-1/result":
            payload = {"run_id": "run-1", "status": "available", "text": "Готовый ответ"}
        else:
            raise AssertionError(f"Unexpected API path: {path}")
        return httpx.Response(200, json=payload)

    server = create_server(
        Settings(url="http://valeria.test", login="mcp-user", password="secret"),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == TOOL_NAMES
        for tool in tools.values():
            assert not {
                "ctx", "login", "password", "user_id", "workspace", "role",
            }.intersection(tool.inputSchema["properties"])
            assert tool.annotations.readOnlyHint == (tool.name != "valeria_create_task_run")
        creation = tools["valeria_create_task_run"].inputSchema
        assert set(creation["required"]) == {"task_id", "idempotency_key"}
        assert creation["properties"]["user_input"]["maxLength"] == 100_000

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result.content
            return result.structuredContent

        catalog = await call("valeria_list_tasks", {})
        assert catalog["tasks"][0]["id"] == "task-1"
        created = await call("valeria_create_task_run", {
            "task_id": "task-1", "idempotency_key": "request-1", "user_input": "Дополнение",
        })
        assert created["id"] == "run-1" and created["status"] == "queued"
        history = await call("valeria_list_task_runs", {})
        assert history["runs"][0]["id"] == "run-1"
        detail = await call("valeria_get_task_run", {"run_id": "run-1"})
        assert detail["status"] == "completed"
        result = await call("valeria_get_task_result", {"run_id": "run-1"})
        assert result["status"] == "available" and result["text"] == "Готовый ответ"
    assert len(requests) == 6


async def test_tool_errors_preserve_validation_and_hide_upstream_body():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"csrf_token": "csrf"})
        return httpx.Response(403, json={"detail": "DO_NOT_EXPOSE_UPSTREAM_SECRET"})

    server = create_server(
        Settings(url="http://valeria.test", login="mcp-user", password="PASSWORD_SECRET"),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        missing_key = await session.call_tool("valeria_create_task_run", {"task_id": "task-1"})
        assert missing_key.isError
        invalid_id = await session.call_tool("valeria_get_task_run", {"run_id": "../other"})
        assert invalid_id.isError
        assert not requests
        denied = await session.call_tool("valeria_list_tasks", {})
        assert denied.isError
        text = " ".join(item.text for item in denied.content)
        assert "403" in text
        assert "DO_NOT_EXPOSE_UPSTREAM_SECRET" not in text
        assert "PASSWORD_SECRET" not in text


async def test_stdio_startup_env_file_and_tool_discovery(tmp_path):
    env_file = tmp_path / "mcp.env"
    env_file.write_text(
        "VALERIA_URL=http://127.0.0.1:9\n"
        "VALERIA_LOGIN=configured-user\n"
        "VALERIA_PASSWORD=configured-password\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "server.py"),
              "--env-file", str(env_file)],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "valeria-tasks"
        assert {tool.name for tool in (await session.list_tools()).tools} == TOOL_NAMES
        result = await session.call_tool("valeria_get_task_run", {"run_id": "../invalid"})
        assert result.isError
        assert "IDs must contain" in result.content[0].text
