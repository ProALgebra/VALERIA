"""OpenCode 1.x HTTP adapter; the legacy wire contract stays here."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from .contracts import OpenCodeError, OpenCodeProtocolError
from .http import HttpBackend


class OpenCodeV1Backend(HttpBackend):
    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/global/health")

    async def path(self) -> dict[str, Any]:
        return await self._request("GET", "/path")

    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None:
        sessions = await self._request("GET", "/session", params={"limit": -1})
        if not isinstance(sessions, list):
            raise OpenCodeProtocolError("OpenCode returned invalid session creation recovery")
        matches = [
            session
            for session in sessions
            if isinstance(session, dict)
            and isinstance(session.get("metadata"), dict)
            and session["metadata"].get("shodanCreationKey") == creation_key
        ]
        if len(matches) > 1:
            raise OpenCodeProtocolError("OpenCode returned multiple sessions for creation key")
        return matches[0] if matches else None

    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title} if title else {}
        body["metadata"] = {"shodanCreationKey": creation_key}
        if permission is not None:
            body["permission"] = permission
        return await self._request(
            "POST",
            "/session",
            json=body,
            **({"before_send": before_send} if before_send is not None else {}),
        )

    async def sessions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/session")
        return result if isinstance(result, list) else []

    async def session(self, session_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/session/{session_id}")
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned invalid session details")
        return result

    async def delete_session(self, session_id: str) -> bool:
        return bool(await self._request("DELETE", f"/session/{session_id}"))

    async def update_session_permissions(
        self,
        session_id: str,
        permission: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Append rules on OpenCode versions whose session PATCH merges permissions."""
        result = await self._request(
            "PATCH",
            f"/session/{session_id}",
            json={"permission": permission},
        )
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned invalid session details")
        return result

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        result = await self._request("GET", f"/session/{session_id}/message")
        if not isinstance(result, list) or any(
            not isinstance(message, dict)
            or not isinstance(message.get("info"), dict)
            or not isinstance(message.get("parts"), list)
            for message in result
        ):
            raise OpenCodeProtocolError("OpenCode returned invalid message history")
        return result

    async def send_prompt_async(
        self,
        session_id: str,
        *,
        message_id: str,
        content: str,
        agent: str,
        provider_id: str | None,
        model_id: str | None,
        model_variant: str | None,
        system: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "messageID": message_id,
            "agent": agent,
            "parts": [{"type": "text", "text": content}],
        }
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
            if model_variant:
                body["variant"] = model_variant
        if system is not None:
            body["system"] = system
        await self._request("POST", f"/session/{session_id}/prompt_async", json=body)

    async def abort(self, session_id: str) -> bool:
        return bool(await self._request("POST", f"/session/{session_id}/abort"))

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str, remember: bool
    ) -> bool:
        result = await self._request(
            "POST",
            f"/session/{session_id}/permissions/{permission_id}",
            json={"response": response, "remember": remember},
        )
        return bool(result)

    async def session_statuses(self) -> dict[str, Any]:
        return await self._request("GET", "/session/status")

    async def permissions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/permission")
        if not isinstance(result, list):
            raise OpenCodeError("OpenCode returned invalid permission requests")
        return result

    async def questions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/question")
        if not isinstance(result, list):
            raise OpenCodeError("OpenCode returned invalid question requests")
        return result

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        return bool(
            await self._request("POST", f"/question/{request_id}/reply", json={"answers": answers})
        )

    async def mcp_statuses(self) -> dict[str, Any]:
        result = await self._request("GET", "/mcp", timeout=30)
        return result if isinstance(result, dict) else {}

    async def global_events(self) -> AsyncIterator[dict[str, Any]]:
        async for event in self._events("/global/event"):
            yield event

    async def providers(self) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        if self._providers_cache is not None and now - self._providers_cache_time < 60:
            return self._providers_cache
        async with self._providers_lock:
            now = asyncio.get_running_loop().time()
            if self._providers_cache is not None and now - self._providers_cache_time < 60:
                return self._providers_cache
            result = await self._request("GET", "/provider")
            if not isinstance(result, dict):
                raise OpenCodeError("OpenCode returned an invalid provider catalog")
            self._providers_cache = result
            self._providers_cache_time = asyncio.get_running_loop().time()
            return result

    async def agents(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/agent")
