"""Exercise administrator tools over MCP, including their executable entrypoint."""

import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from admin_config import Settings
from admin_operations import OPERATIONS
from server import create_server


async def test_mcp_creates_user_and_task_and_reads_queue():
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            assert json.loads(request.content) == {"login": "administrator", "password": "secret"}
            return httpx.Response(
                200, json={"user": {"role": "admin"}, "csrf_token": "csrf"},
                headers={"set-cookie": "ocw_session=session; Path=/"},
            )
        assert request.headers["cookie"] == "ocw_session=session"
        assert request.headers["x-csrf-token"] == "csrf"
        if path == "/api/admin/users":
            body = json.loads(request.content)
            assert body == {
                "login": "worker", "password": "worker-long-password",
                "workspace_path": "team/project", "tasks_only": True,
            }
            return httpx.Response(201, json={"id": "worker", "login": "worker", "role": "user"})
        if path == "/api/admin/tasks":
            assert json.loads(request.content) == {
                "title": "Task", "content": "Instructions", "agent": "build",
                "model": "provider/coder",
            }
            return httpx.Response(201, json={"id": "task", "title": "Task"})
        assert path == "/api/admin/queue"
        return httpx.Response(200, json={"limit": 2, "jobs": [], "queues": []})

    server = create_server(
        Settings(url="http://valeria.test", login="administrator", password="secret"),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == set(OPERATIONS)
        assert tools["valeria_admin_get_queue"].annotations.readOnlyHint
        assert tools["valeria_admin_delete_task"].annotations.destructiveHint
        assert not tools["valeria_admin_create_user"].annotations.idempotentHint
        assert not tools["valeria_admin_reset_user_password"].annotations.idempotentHint
        for name, arguments, expected in (
            ("valeria_admin_create_user", {
                "login": "worker", "password": "worker-long-password",
                "workspace_path": "team/project", "tasks_only": True,
            }, {"id": "worker", "login": "worker", "role": "user"}),
            ("valeria_admin_create_task", {
                "title": "Task", "content": "Instructions", "agent": "build",
                "model": "provider/coder",
            }, {"id": "task", "title": "Task"}),
            ("valeria_admin_get_queue", {}, {"limit": 2, "jobs": [], "queues": []}),
        ):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result.content
            assert result.structuredContent == expected
    assert len(requests) == 4


async def test_invalid_password_and_unknown_tool_arguments_do_not_leak_values_or_call_api():
    requests = []

    def handle(request):
        requests.append(request)
        raise AssertionError("Invalid input must not call Valeria")

    server = create_server(
        Settings(url="http://valeria.test", login="administrator", password="AUTH_SECRET"),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("valeria_admin_create_user", {
            "login": "worker", "password": "TOO_SHORT",
        })
        assert result.isError
        assert result.structuredContent["error"]["fields"] == [
            {"field": "password", "type": "string_too_short"},
        ]
        assert "TOO_SHORT" not in result.content[0].text
        assert "AUTH_SECRET" not in result.content[0].text
        unknown_field = await session.call_tool("valeria_admin_get_queue", {"url": "SECRET_URL"})
        assert unknown_field.isError
        assert "SECRET_URL" not in unknown_field.content[0].text
        unknown_tool = await session.call_tool("valeria_admin_request", {"path": "api/profile"})
        assert unknown_tool.isError
    assert not requests


async def test_nonadministrator_cannot_use_shared_read_endpoints_through_admin_mcp():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"user": {"role": "user"}, "csrf_token": "csrf"})

    server = create_server(
        Settings(url="http://valeria.test", login="worker", password="secret"),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("valeria_admin_get_public_config", {})
        assert result.isError
        assert result.structuredContent["error"]["status_code"] == 403
    assert paths == ["/api/auth/login"]


async def test_stdio_entrypoint_loads_admin_configuration_and_exposes_tools(tmp_path):
    env_file = tmp_path / "admin.env"
    env_file.write_text(
        "VALERIA_ADMIN_URL=http://127.0.0.1:9\n"
        "VALERIA_ADMIN_LOGIN=administrator\n"
        "VALERIA_ADMIN_PASSWORD=administrator-password\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "server.py"),
              "--env-file", str(env_file)],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "valeria-administrator"
        assert {tool.name for tool in (await session.list_tools()).tools} == set(OPERATIONS)
        result = await session.call_tool("valeria_admin_get_job", {"job_id": "../invalid"})
        assert result.isError
        assert result.structuredContent["error"]["message"] == "Invalid tool arguments"
