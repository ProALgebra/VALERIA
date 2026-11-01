"""Minimal Telegram Bot API transport without logging credentials or message bodies."""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .config import Settings

Keyboard = list[list[dict[str, Any]]]
_CHUNK_UNITS = 3900
_DOCUMENT_THRESHOLD_UNITS = 12000


class TelegramError(Exception):
    """Only safe numeric API metadata is exposed to callers and logs."""

    def __init__(self, error_code: int | None = None, retry_after: int | None = None):
        self.error_code = error_code
        self.retry_after = retry_after
        message = "Telegram request failed"
        if error_code is not None:
            message += f" (code {error_code})"
        super().__init__(message)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _text_chunks(value: str, limit: int = _CHUNK_UNITS) -> list[str]:
    """Split at Unicode code points while observing Telegram's UTF-16 lengths."""
    chunks: list[str] = []
    start = 0
    units = 0
    for index, char in enumerate(value):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(value[start:index])
            start = index
            units = 0
        units += width
    if start < len(value):
        chunks.append(value[start:])
    return chunks


def _keyboard_markup(keyboard: Keyboard | None) -> dict[str, Keyboard] | None:
    if keyboard is None:
        return None
    for row in keyboard:
        for button in row:
            callback = button.get("callback_data")
            if callback is not None and (
                not isinstance(callback, str) or not 1 <= len(callback.encode("utf-8")) <= 64
            ):
                raise ValueError("Telegram callback_data must contain 1 to 64 UTF-8 bytes")
    return {"inline_keyboard": keyboard}


class TelegramClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        send_interval: float = 1.0,
    ):
        self._base_url = f"https://api.telegram.org/bot{settings.bot_token}/"
        self._timeout = settings.request_timeout_seconds
        verify = ssl.create_default_context()
        if settings.ca_bundle:
            verify.load_verify_locations(cafile=str(settings.ca_bundle))
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            verify=verify,
            follow_redirects=False,
            transport=transport,
        )
        self._send_interval = send_interval
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_sent: dict[int, float] = {}

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        read_timeout: float | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        # Retrying a send after a network failure could deliver the message twice.
        # Retry only an explicit 429 rejection, which has not accepted the send.
        for attempt in range(3):
            kwargs: dict[str, Any] = {}
            if read_timeout is not None:
                kwargs["timeout"] = httpx.Timeout(self._timeout, read=read_timeout)
            if files is None:
                kwargs["json"] = payload
            else:
                kwargs["data"] = payload
                kwargs["files"] = files
            try:
                response = await self._client.post(self._base_url + method, **kwargs)
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise TelegramError() from None
            if not isinstance(data, dict):
                raise TelegramError()
            if response.is_success and data.get("ok") is True:
                return data.get("result")
            error_code = data.get("error_code")
            if not isinstance(error_code, int) or isinstance(error_code, bool):
                error_code = response.status_code
            parameters = data.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 0:
                retry_after = None
            error = TelegramError(error_code, retry_after)
            if error_code == 429 and retry_after is not None and retry_after <= 300 and attempt < 2:
                await asyncio.sleep(retry_after)
                continue
            raise error
        raise TelegramError()  # Defensive: every loop branch above returns or raises.

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            read_timeout=max(self._timeout, timeout + 10),
        )
        if not isinstance(result, list) or any(
            not isinstance(update, dict)
            or not isinstance(update.get("update_id"), int)
            or isinstance(update.get("update_id"), bool)
            for update in result
        ):
            raise TelegramError()
        return result

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramError()
        return result

    async def get_webhook_info(self) -> dict[str, Any]:
        result = await self._call("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramError()
        return result

    async def _rate_limit(self, chat_id: int) -> None:
        delay = self._last_sent.get(chat_id, 0) + self._send_interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_sent[chat_id] = time.monotonic()

    async def send_text(self, chat_id: int, text: str, *, keyboard: Keyboard | None = None) -> None:
        markup = _keyboard_markup(keyboard)
        if not text:
            raise ValueError("Telegram message text must not be empty")
        async with self._chat_locks.setdefault(chat_id, asyncio.Lock()):
            if _utf16_units(text) > _DOCUMENT_THRESHOLD_UNITS:
                await self._rate_limit(chat_id)
                await self._call(
                    "sendDocument",
                    {"chat_id": str(chat_id), "caption": "Полный текст сообщения"},
                    files={"document": ("valeria-message.txt", text.encode("utf-8"), "text/plain")},
                )
                preview = _text_chunks(text, 3700)[0] + "\n\nПолный текст — в приложенном файле."
                chunks = [preview]
            else:
                chunks = _text_chunks(text)
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if markup is not None and index == len(chunks) - 1:
                    payload["reply_markup"] = markup
                await self._rate_limit(chat_id)
                await self._call("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = _text_chunks(text, 190)[0]
        await self._call("answerCallbackQuery", payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
