"""HTTP transport shared by the two independently versioned adapters."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .contracts import (
    OpenCodeError,
    OpenCodeProtocolError,
    OpenCodeReadTimeout,
    OpenCodeTransportError,
)
from .creation import SessionCreation


class HttpBackend(SessionCreation):
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        workspace: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self._providers_cache: dict[str, Any] | None = None
        self._providers_cache_time = 0.0
        self._providers_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(username, password) if password else None,
            timeout=httpx.Timeout(30, connect=5, read=2 * 60 * 60),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        before_send: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Any:
        if before_send is not None and not before_send():
            raise asyncio.CancelledError
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            if len(detail) > 2_000:
                detail = detail[:2_000] + "…"
            suffix = f": {detail}" if detail else ""
            raise OpenCodeError(
                f"OpenCode returned HTTP {exc.response.status_code} for {method} {path}{suffix}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.ReadTimeout as exc:
            raise OpenCodeReadTimeout(f"OpenCode response timed out: {method} {path}") from exc
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ) as exc:
            raise OpenCodeTransportError(
                f"OpenCode transport failed: {method} {path} ({type(exc).__name__})",
                request_not_sent=isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"OpenCode request failed: {method} {path}") from exc
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeProtocolError(f"OpenCode returned invalid JSON: {method} {path}") from exc

    async def _events(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream("GET", path, params=params, timeout=None) as response:
                response.raise_for_status()
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        raw = "\n".join(data)
                        data.clear()
                        try:
                            event = json.loads(raw)
                        except ValueError as exc:
                            raise OpenCodeProtocolError(
                                "OpenCode returned an invalid event"
                            ) from exc
                        if not isinstance(event, dict):
                            raise OpenCodeProtocolError("OpenCode returned an invalid event")
                        yield event
        except httpx.HTTPError as exc:
            raise OpenCodeError("OpenCode event stream failed") from exc
