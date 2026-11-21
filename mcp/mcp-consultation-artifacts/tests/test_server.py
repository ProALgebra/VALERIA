from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import Settings, SettingsError, create_server

TOKEN = "t" * 32
META = {"ai.opencode/sessionID": "session-1"}


@pytest.fixture
def requests():
    return []


@pytest.fixture
def server(requests):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/internal/consultation-artifacts"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        assert payload["session_id"] == "session-1"
        if "path" in payload:
            assert payload["path"] == "/app/data/consultation-staging/chat/job/outbox/report.txt"
        else:
            assert payload["text"] == "hello"
        return httpx.Response(
            200,
            json={
                "id": "artifact-1",
                "name": payload.get("name") or "report.txt",
                "content_type": "text/plain",
                "size_bytes": 5,
                "description": payload.get("description"),
                "status": "pending",
                "download_url": "/api/consultations/chat/artifacts/artifact-1",
                "chat_id": "chat",
                "job_id": "job",
            },
        )

    return create_server(
        Settings(url="http://valeria.test", token=TOKEN),
        transport=httpx.MockTransport(handle),
    )


async def test_tools_publish_files_and_text_with_opencode_session_meta(server, requests):
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"artifact_publish", "artifact_publish_text"}
        for tool in tools.values():
            assert tool.annotations.readOnlyHint is False
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.idempotentHint is True
            assert "session_id" not in tool.inputSchema["properties"]
        assert set(tools["artifact_publish"].inputSchema["required"]) == {"path"}
        assert set(tools["artifact_publish_text"].inputSchema["required"]) == {
            "name",
            "text",
        }

        result = await session.call_tool(
            "artifact_publish",
            {
                "path": "/app/data/consultation-staging/chat/job/outbox/report.txt",
                "name": "report.txt",
            },
            meta=META,
        )
        assert not result.isError, result.content
        assert result.structuredContent["id"] == "artifact-1"

        result = await session.call_tool(
            "artifact_publish_text",
            {"name": "note.txt", "text": "hello"},
            meta=META,
        )
        assert not result.isError, result.content
        assert result.structuredContent["status"] == "pending"

    assert len(requests) == 2


async def test_missing_session_metadata_is_a_tool_error(server, requests):
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "artifact_publish_text", {"name": "note.txt", "text": "hello"}
        )
    assert result.isError
    text = " ".join(item.text for item in result.content)
    assert "session metadata" in text.lower()
    assert requests == []


async def test_upstream_body_and_token_are_not_exposed():
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": "DO_NOT_EXPOSE_UPSTREAM_SECRET"},
        )

    server = create_server(
        Settings(url="http://valeria.test", token=TOKEN),
        transport=httpx.MockTransport(handle),
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "artifact_publish_text",
            {"name": "note.txt", "text": "hello"},
            meta=META,
        )
    assert result.isError
    text = " ".join(item.text for item in result.content)
    assert "403" in text
    assert "DO_NOT_EXPOSE_UPSTREAM_SECRET" not in text
    assert TOKEN not in text


def test_settings_resolve_token_from_file(tmp_path):
    token_file = tmp_path / "artifact.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    settings = Settings(url="http://valeria.test", token_file=token_file)
    assert settings.resolve_token() == TOKEN


def test_settings_reject_short_or_missing_token(tmp_path):
    with pytest.raises(SettingsError):
        Settings(url="http://valeria.test", token="short").resolve_token()
    with pytest.raises(SettingsError):
        Settings(url="http://valeria.test", token_file=tmp_path / "missing").resolve_token()
