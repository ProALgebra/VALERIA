"""Per-user HTTP sessions for the existing Valeria user API."""

from __future__ import annotations

import ssl
from types import TracebackType
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import httpx

if TYPE_CHECKING:
    from .config import Settings

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_METHODS = _READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}
_ERROR_MESSAGES = {
    401: "Сессия истекла. Войдите в Valeria снова.",
    403: "Действие недоступно для вашей учётной записи.",
    404: "Таска или диалог недоступны.",
    409: "Состояние изменилось. Обновите данные и повторите действие.",
    422: "Проверьте параметры. Для работы нужна назначенная рабочая папка.",
    429: "Очередь заполнена. Повторите попытку позже.",
    503: "Valeria временно недоступна. Повторите попытку позже.",
}


class ValeriaError(Exception):
    """Safe user-visible error, with enough metadata for durable submission retries."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        idempotency_key: str | None = None,
        acceptance_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.idempotency_key = idempotency_key
        self.acceptance_unknown = acceptance_unknown


def _token(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) < 33 or ord(char) > 126 or char in '\\";,' for char in value)
    ):
        raise ValeriaError("Некорректные данные сессии. Войдите в Valeria снова.")
    return value


def _api_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 2048:
        raise ValeriaError("Некорректный путь API Valeria.")
    # Reject encoded separators/control characters too; httpx/server must not
    # normalize a different route after the client has checked its destination.
    decoded = path
    for _ in range(4):
        if (
            any(ord(char) < 33 or ord(char) > 126 for char in decoded)
            or "\\" in decoded
            or "?" in decoded
            or "#" in decoded
            or ":" in decoded
            or decoded.startswith("//")
            or "%2f" in decoded.lower()
            or "%5c" in decoded.lower()
        ):
            raise ValeriaError("Некорректный путь API Valeria.")
        segments = decoded.lstrip("/").split("/")
        if segments[0] != "api" or any(part in {"", ".", ".."} for part in segments):
            raise ValeriaError("Некорректный путь API Valeria.")
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            return decoded.lstrip("/")
        decoded = next_decoded
    raise ValeriaError("Некорректный путь API Valeria.")


class ValeriaClient:
    """Own cookies belong to exactly one logged-in Telegram user's session."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_token: str | None = None,
        csrf_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_url = settings.valeria_url
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in raw_url)
        ):
            raise ValeriaError("Некорректный адрес Valeria.")
        self.session_token = _token(session_token)
        self.csrf_token = _token(csrf_token)
        verify = ssl.create_default_context(
            cafile=str(settings.ca_bundle) if settings.ca_bundle else None
        )
        self.client = httpx.AsyncClient(
            base_url=raw_url.rstrip("/") + "/",
            timeout=settings.request_timeout_seconds,
            verify=verify,
            follow_redirects=False,
            transport=transport,
            cookies={"ocw_session": self.session_token} if self.session_token else None,
        )

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
        await self.client.aclose()

    async def login(self, login: str, password: str) -> dict[str, Any]:
        if (
            not isinstance(login, str)
            or not login.strip()
            or len(login) > 80
            or not isinstance(password, str)
            or not password
            or len(password) > 1024
        ):
            raise ValeriaError("Введите логин и пароль Valeria.", status_code=422)
        # A failed login must not accidentally leave an earlier user's cookies.
        self.client.cookies.clear()
        self.session_token = self.csrf_token = None
        try:
            response = await self._send(
                "POST",
                "api/auth/login",
                body={"login": login, "password": password},
                expected_status=200,
            )
        except ValeriaError as exc:
            self.client.cookies.clear()
            if exc.status_code == 401:
                raise ValeriaError("Неверный логин или пароль Valeria.", status_code=401) from None
            raise
        try:
            data = response.json()
            user = data["user"]
            csrf = _token(data["csrf_token"])
            session = _token(response.cookies.get("ocw_session"))
            if (
                not csrf
                or not session
                or not isinstance(user, dict)
                or not isinstance(user.get("id"), str)
                or not user["id"]
                or not isinstance(user.get("login"), str)
                or not user["login"]
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, httpx.CookieConflict, ValeriaError):
            self.client.cookies.clear()
            raise ValeriaError("Valeria вернула некорректные данные входа.") from None
        self.session_token, self.csrf_token = session, csrf
        return {"session_token": session, "csrf_token": csrf, "user": user}

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected_status: int = 200,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        response = await self._send(
            method,
            path,
            body=body,
            params=params,
            expected_status=expected_status,
            idempotency_key=idempotency_key,
        )
        if response.status_code == 204:
            return {"ok": True}
        try:
            data = response.json()
            if not isinstance(data, dict | list):
                raise ValueError
            return data
        except ValueError:
            raise ValeriaError(
                "Valeria вернула некорректный ответ. Обновите состояние запроса.",
                status_code=response.status_code,
                idempotency_key=idempotency_key,
                acceptance_unknown=method.upper() not in _READ_METHODS,
            ) from None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected_status: int,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        method = method.upper()
        if method not in _METHODS:
            raise ValeriaError("Некорректный метод API Valeria.")
        path = _api_path(path)
        headers = {}
        if self.csrf_token and method not in _READ_METHODS:
            headers["X-CSRF-Token"] = self.csrf_token
        if idempotency_key is not None:
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key
                or len(idempotency_key) > 200
                or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key)
            ):
                raise ValeriaError("Некорректный ключ повтора запроса.")
            headers["Idempotency-Key"] = idempotency_key
        mutating = method not in _READ_METHODS
        try:
            response = await self.client.request(
                method,
                path,
                json=body,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError:
            raise ValeriaError(
                "Нет связи с Valeria. Обновите состояние и повторите попытку.",
                idempotency_key=idempotency_key,
                acceptance_unknown=mutating,
            ) from None
        if response.status_code != expected_status:
            message = _ERROR_MESSAGES.get(
                response.status_code,
                "Valeria не выполнила запрос. Повторите попытку позже.",
            )
            raise ValeriaError(
                message,
                status_code=response.status_code,
                idempotency_key=idempotency_key,
                acceptance_unknown=mutating
                and (
                    response.status_code >= 500
                    or response.status_code == 408
                    or 200 <= response.status_code < 400
                ),
            )
        return response
