"""Cookie-authenticated client for Valeria's existing administrator HTTP API."""

from __future__ import annotations

import asyncio
import re
import ssl
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx

from admin_config import ConfigError, Settings

_API_PATH = re.compile(r"api/[A-Za-z0-9_/-]+\Z")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AdminError(RuntimeError):
    """A safe error without upstream response bodies, credentials, or cookies."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.idempotency_key = idempotency_key
        if idempotency_key is not None:
            message += f"; Idempotency-Key: {idempotency_key}"
        super().__init__(message)


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(not 32 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError("Idempotency-Key must be nonblank printable ASCII, at most 200 characters")
    return value


class AdminClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        verify: bool | ssl.SSLContext = True
        if settings.ca_bundle is not None:
            try:
                verify = ssl.create_default_context(cafile=str(settings.ca_bundle))
            except (OSError, ValueError):
                raise ConfigError("Cannot load VALERIA_ADMIN_CA_BUNDLE") from None
        self._client = httpx.AsyncClient(
            base_url=settings.url.rstrip("/") + "/",
            timeout=settings.request_timeout_seconds,
            verify=verify,
            follow_redirects=False,
            transport=transport,
        )
        self._csrf_token: str | None = None
        self._auth_lock = asyncio.Lock()

    async def __aenter__(self) -> AdminClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        """Called only while holding the authentication lock."""
        self._csrf_token = None
        self._client.cookies.clear()
        try:
            response = await self._client.post(
                "api/auth/login",
                json={"login": self.settings.login, "password": self.settings.password},
            )
        except httpx.HTTPError:
            raise AdminError("Cannot connect to Valeria for administrator login") from None
        if response.status_code != 200:
            raise AdminError(
                f"Valeria administrator login failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("user"), dict):
            self._client.cookies.clear()
            raise AdminError("Valeria login returned an invalid user", status_code=200)
        if payload["user"].get("role") != "admin":
            self._client.cookies.clear()
            raise AdminError("Configured Valeria account must be an administrator", status_code=403)
        token = payload.get("csrf_token")
        if (
            not isinstance(token, str)
            or not token
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            self._client.cookies.clear()
            raise AdminError("Valeria login returned an invalid CSRF token", status_code=200)
        self._csrf_token = token
        return token

    async def _authenticate(self, rejected_token: str | None = None) -> str:
        async with self._auth_lock:
            if self._csrf_token is None or self._csrf_token == rejected_token:
                return await self._login()
            return self._csrf_token

    @staticmethod
    def _failure_message(message: str, method: str, idempotency_key: str | None) -> str:
        if method in _WRITES:
            if idempotency_key is not None:
                return (
                    message + "; submission acceptance is unknown, reuse the same Idempotency-Key"
                )
            return message + "; mutation acceptance is unknown, inspect state before retrying"
        return message

    async def request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int = 200,
        body: dict[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Call a fixed API endpoint; never follow redirects or replay failed writes."""
        if method not in _METHODS:
            raise ValueError("Unsupported API method")
        if not isinstance(path, str) or not _API_PATH.fullmatch(path) or "//" in path:
            raise ValueError("Expected a relative Valeria api/ path")
        if idempotency_key is not None:
            idempotency_key = _idempotency_key(idempotency_key)
        token = await self._authenticate()
        headers = {"X-CSRF-Token": token}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(
                method, path, headers=headers, json=body, params=params
            )
        except httpx.HTTPError:
            raise AdminError(
                self._failure_message("Cannot connect to Valeria", method, idempotency_key),
                idempotency_key=idempotency_key,
            ) from None
        if response.status_code == 401:
            headers["X-CSRF-Token"] = await self._authenticate(rejected_token=token)
            try:
                response = await self._client.request(
                    method, path, headers=headers, json=body, params=params
                )
            except httpx.HTTPError:
                raise AdminError(
                    self._failure_message("Cannot reconnect to Valeria", method, idempotency_key),
                    idempotency_key=idempotency_key,
                ) from None
        if response.status_code != expected_status:
            message = f"Valeria request failed with HTTP {response.status_code}"
            if response.status_code >= 500:
                message = self._failure_message(message, method, idempotency_key)
            raise AdminError(
                message,
                status_code=response.status_code,
                idempotency_key=idempotency_key,
            )
        if response.status_code == 204:
            return {"ok": True}
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, (dict, list)):
            raise AdminError(
                self._failure_message(
                    "Valeria returned an invalid JSON response", method, idempotency_key
                ),
                status_code=response.status_code,
                idempotency_key=idempotency_key,
            )
        return payload
