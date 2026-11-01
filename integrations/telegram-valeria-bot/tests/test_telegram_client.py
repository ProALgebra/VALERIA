from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from valeria_telegram.config import Settings
from valeria_telegram.telegram_client import TelegramClient, TelegramError


def settings() -> Settings:
    return Settings("123:secret-token", "https://valeria.example", Path("unused.db"))


async def test_long_poll_timeout_and_update_types():
    def handler(request):
        assert request.url.path.endswith("/getUpdates")
        assert json.loads(request.content) == {
            "offset": 123,
            "timeout": 50,
            "allowed_updates": ["message", "callback_query"],
        }
        assert request.extensions["timeout"]["read"] >= 60
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 123}]})

    async with TelegramClient(settings(), transport=httpx.MockTransport(handler)) as client:
        assert await client.get_updates(123, 50) == [{"update_id": 123}]


async def test_invalid_update_response_rejected():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True, "result": [{}]})
    )
    async with TelegramClient(settings(), transport=transport) as client:
        with pytest.raises(TelegramError):
            await client.get_updates(0, 1)


async def test_message_chunks_preserve_all_text_and_utf16_limits():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    text = "A🌍" * 3500
    keyboard = [[{"text": "Меню", "callback_data": "menu"}]]
    async with TelegramClient(
        settings(), transport=httpx.MockTransport(handler), send_interval=0
    ) as client:
        await client.send_text(12, text, keyboard=keyboard)
    assert "".join(payload["text"] for payload in requests) == text
    assert all(len(payload["text"].encode("utf-16-le")) // 2 <= 4096 for payload in requests)
    assert all("reply_markup" not in payload for payload in requests[:-1])
    assert requests[-1]["reply_markup"] == {"inline_keyboard": keyboard}
    assert all("parse_mode" not in payload for payload in requests)


async def test_huge_response_delivered_as_complete_document_with_preview_and_controls():
    requests = []

    async def handler(request):
        requests.append((request.url.path, await request.aread()))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    text = "🌍Ответ <b>без форматирования</b>\n" * 1000
    keyboard = [[{"text": "Меню", "callback_data": "menu"}]]
    async with TelegramClient(
        settings(), transport=httpx.MockTransport(handler), send_interval=0
    ) as client:
        await client.send_text(12, text, keyboard=keyboard)
    assert len(requests) == 2
    assert requests[0][0].endswith("/sendDocument")
    assert text.encode("utf-8") in requests[0][1]
    assert b'filename="valeria-message.txt"' in requests[0][1]
    preview = json.loads(requests[1][1])
    assert len(preview["text"].encode("utf-16-le")) // 2 <= 4096
    assert preview["reply_markup"] == {"inline_keyboard": keyboard}


@pytest.mark.parametrize("callback", ["", "x" * 65, "🌍" * 17, 123])
async def test_invalid_callbacks_rejected_before_network(callback):
    def handler(request):
        raise AssertionError("Network must not be called")

    async with TelegramClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="callback_data"):
            await client.send_text(1, "hello", keyboard=[[{"callback_data": callback}]])


async def test_explicit_rate_limit_retried_with_retry_after(monkeypatch):
    calls = 0
    delays = []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr("valeria_telegram.telegram_client.asyncio.sleep", sleep)
    async with TelegramClient(
        settings(), transport=httpx.MockTransport(handler), send_interval=0
    ) as client:
        await client.send_text(1, "hello")
    assert calls == 2
    assert delays == [2]


async def test_rate_limit_attempts_bounded_and_metadata_safe(monkeypatch):
    calls = 0

    async def sleep(delay):
        pass

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "secret-token private prompt",
                "parameters": {"retry_after": 3},
            },
        )

    monkeypatch.setattr("valeria_telegram.telegram_client.asyncio.sleep", sleep)
    async with TelegramClient(
        settings(), transport=httpx.MockTransport(handler), send_interval=0
    ) as client:
        with pytest.raises(TelegramError) as caught:
            await client.send_text(1, "private prompt")
    assert calls == 3
    assert caught.value.error_code == 429
    assert caught.value.retry_after == 3
    assert "secret-token" not in str(caught.value)
    assert "private prompt" not in str(caught.value)


async def test_send_network_errors_are_not_retried_or_exposed():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(f"URL {request.url} private prompt")

    async with TelegramClient(
        settings(), transport=httpx.MockTransport(handler), send_interval=0
    ) as client:
        with pytest.raises(TelegramError) as caught:
            await client.send_text(1, "private prompt")
    assert calls == 1
    assert str(caught.value) == "Telegram request failed"


async def test_callback_answer_and_delete_are_plain_api_calls():
    requests = []

    def handler(request):
        requests.append((request.url.path.rsplit("/", 1)[1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with TelegramClient(settings(), transport=httpx.MockTransport(handler)) as client:
        await client.answer_callback_query("cb-id", "Получено")
        await client.delete_message(1, 22)
    assert requests == [
        ("answerCallbackQuery", {"callback_query_id": "cb-id", "text": "Получено"}),
        ("deleteMessage", {"chat_id": 1, "message_id": 22}),
    ]
