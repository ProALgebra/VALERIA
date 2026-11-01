from __future__ import annotations

import asyncio
from pathlib import Path

from valeria_telegram.config import Settings
from valeria_telegram.runner import process_updates, run_bot


def update(update_id, user_id):
    return {"update_id": update_id, "message": {"from": {"id": user_id}}}


async def test_groups_preserve_user_order_and_allow_other_user_progress():
    order = []
    second_user_done = asyncio.Event()

    class Bot:
        async def handle_update(self, item):
            identifier = item["update_id"]
            if identifier == 1:
                await asyncio.wait_for(second_user_done.wait(), 1)
            order.append(identifier)
            if identifier == 2:
                second_user_done.set()

    assert await process_updates(Bot(), [update(3, 10), update(2, 20), update(1, 10)])
    assert order == [2, 1, 3]


async def test_failed_user_does_not_block_others_or_consume_following_update(caplog):
    processed = []

    class Bot:
        async def handle_update(self, item):
            if item["update_id"] == 1:
                raise RuntimeError("secret password")
            processed.append(item["update_id"])

    assert not await process_updates(Bot(), [update(1, 10), update(2, 20), update(3, 10)])
    assert processed == [2]
    assert "RuntimeError" in caplog.text
    assert "secret password" not in caplog.text


async def test_successful_batch_advances_offset_and_notifies_independently():
    stop_event = asyncio.Event()
    notified = asyncio.Event()

    class Store:
        offset = 0

        def get_offset(self):
            return self.offset

        def set_offset(self, offset):
            self.offset = offset
            stop_event.set()

    class Bot:
        async def handle_update(self, item):
            await asyncio.wait_for(notified.wait(), 1)

        async def poll_notifications(self):
            notified.set()

    class Telegram:
        async def get_updates(self, offset, timeout):
            assert offset == 0
            return [update(6, 10), update(7, 10)]

    store = Store()
    settings = Settings("123:token", "https://valeria.example", Path("unused.db"))
    await asyncio.wait_for(run_bot(Bot(), store, Telegram(), settings, stop_event=stop_event), 1)
    assert store.offset == 8


async def test_failed_batch_replayed_without_advancing_offset():
    stop_event = asyncio.Event()
    calls = []

    class Store:
        def get_offset(self):
            return 10

        def set_offset(self, offset):
            raise AssertionError("Failed batch must not advance the offset")

    class Bot:
        async def handle_update(self, item):
            raise ValueError("private text")

        async def poll_notifications(self):
            pass

    class Telegram:
        async def get_updates(self, offset, timeout):
            calls.append(offset)
            if len(calls) == 2:
                stop_event.set()
            return [update(10, 10)]

    settings = Settings(
        "123:token", "https://valeria.example", Path("unused.db"), poll_interval_seconds=0.001
    )
    await asyncio.wait_for(run_bot(Bot(), Store(), Telegram(), settings, stop_event=stop_event), 1)
    assert calls == [10, 10]
