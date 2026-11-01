"""Cookie-authenticated client for Valeria's existing user task HTTP API."""

from __future__ import annotations

import asyncio
import re
import ssl
from types import TracebackType
from typing import Any

import httpx

from config import ConfigError, Settings

RUN_STATUSES = frozenset(
    {"queued", "dispatching", "running", "cancelling", "completed", "failed", "cancelled"}
)
RESULT_STATUSES = frozenset({"not_completed", "available", "unavailable"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+\Z")


class ValeriaError(RuntimeError):
    """A safe client error, without upstream bodies, cookies, or credentials."""

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


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Task and run IDs must contain only ASCII letters, digits, '_' or '-'")
    return value


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(not 32 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError("Idempotency-Key must be nonblank printable ASCII, at most 200 characters")
    return value


class ValeriaClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        verify: bool | ssl.SSLContext = True
        if settings.ca_bundle is not None:
            try:
                verify = ssl.create_default_context(cafile=str(settings.ca_bundle))
            except (OSError, ValueError):
                raise ConfigError("Cannot load VALERIA_CA_BUNDLE") from None
        self._client = httpx.AsyncClient(
            base_url=settings.url.rstrip("/") + "/",
            timeout=settings.request_timeout_seconds,
            verify=verify,
            follow_redirects=False,
            transport=transport,
        )
        self._csrf_token: str | None = None
        self._auth_lock = asyncio.Lock()

    async def __aenter__(self) -> ValeriaClient:
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
        try:
            response = await self._client.post(
                "api/auth/login",
                json={"login": self.settings.login, "password": self.settings.password},
            )
        except httpx.HTTPError:
            raise ValeriaError("Cannot connect to Valeria for login") from None
        if response.status_code != 200:
            raise ValeriaError(
                f"Valeria login failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
            token = payload.get("csrf_token") if isinstance(payload, dict) else None
        except ValueError:
            token = None
        if (
            not isinstance(token, str)
            or not token
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ValeriaError("Valeria login returned an invalid response", status_code=200)
        self._csrf_token = token
        return token

    async def _authenticate(self, rejected_token: str | None = None) -> str:
        async with self._auth_lock:
            if self._csrf_token is None or self._csrf_token == rejected_token:
                return await self._login()
            return self._csrf_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        token = await self._authenticate()
        headers = {"X-CSRF-Token": token}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(method, path, headers=headers, json=body)
        except httpx.HTTPError:
            raise ValeriaError(
                "Cannot connect to Valeria; submission acceptance is unknown"
                if idempotency_key is not None else "Cannot connect to Valeria",
                idempotency_key=idempotency_key,
            ) from None
        if response.status_code == 401:
            headers["X-CSRF-Token"] = await self._authenticate(rejected_token=token)
            try:
                response = await self._client.request(method, path, headers=headers, json=body)
            except httpx.HTTPError:
                raise ValeriaError(
                    "Cannot reconnect to Valeria; submission acceptance is unknown"
                    if idempotency_key is not None else "Cannot reconnect to Valeria",
                    idempotency_key=idempotency_key,
                ) from None
        if response.status_code != expected_status:
            raise ValeriaError(
                f"Valeria request failed with HTTP {response.status_code}",
                status_code=response.status_code,
                idempotency_key=idempotency_key,
            )
        try:
            return response.json()
        except ValueError:
            raise ValeriaError(
                "Valeria returned an invalid JSON response",
                status_code=response.status_code,
                idempotency_key=idempotency_key,
            ) from None

    @staticmethod
    def _run(payload: Any, *, idempotency_key: str | None = None) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not payload["id"]
            or not isinstance(payload.get("status"), str)
            or payload["status"] not in RUN_STATUSES
        ):
            raise ValeriaError(
                "Valeria returned an invalid task run", idempotency_key=idempotency_key
            )
        return payload

    async def list_tasks(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "api/tasks", expected_status=200)
        if not isinstance(payload, list) or any(
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not task["id"]
            or not isinstance(task.get("title"), str)
            for task in payload
        ):
            raise ValeriaError("Valeria returned an invalid task catalog")
        return payload

    async def create_task_run(
        self, task_id: str, user_input: str = "", *, idempotency_key: str
    ) -> dict[str, Any]:
        task_id = _identifier(task_id)
        key = _idempotency_key(idempotency_key)
        if not isinstance(user_input, str) or len(user_input) > 100_000:
            raise ValueError("user_input must be a string of at most 100000 characters")
        payload = await self._request(
            "POST", f"api/tasks/{task_id}/runs", expected_status=202,
            body={"user_input": user_input}, idempotency_key=key,
        )
        return self._run(payload, idempotency_key=key)

    async def list_task_runs(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "api/task-runs", expected_status=200)
        if not isinstance(payload, list):
            raise ValeriaError("Valeria returned an invalid task run list")
        return [self._run(run) for run in payload]

    async def get_task_run(self, run_id: str) -> dict[str, Any]:
        run_id = _identifier(run_id)
        payload = await self._request("GET", f"api/task-runs/{run_id}", expected_status=200)
        return self._run(payload)

    async def get_task_result(self, run_id: str) -> dict[str, Any]:
        run_id = _identifier(run_id)
        payload = await self._request(
            "GET", f"api/task-runs/{run_id}/result", expected_status=200
        )
        if (
            not isinstance(payload, dict)
            or payload.get("run_id") != run_id
            or not isinstance(payload.get("status"), str)
            or payload["status"] not in RESULT_STATUSES
            or "text" not in payload
            or (payload["text"] is not None and not isinstance(payload["text"], str))
        ):
            raise ValeriaError("Valeria returned an invalid task result")
        return payload
