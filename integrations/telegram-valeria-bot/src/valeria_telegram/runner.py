"""Long polling with durable offsets and independent background notifications."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from .telegram_client import TelegramError

if TYPE_CHECKING:
    from .bot import ValeriaBot
    from .config import Settings
    from .store import Store
    from .telegram_client import TelegramClient

logger = logging.getLogger(__name__)


async def _pause(stop_event: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


def _sender(update: dict[str, Any]) -> object:
    for key in ("message", "callback_query"):
        item = update.get(key)
        if isinstance(item, dict):
            sender = item.get("from")
            if isinstance(sender, dict) and isinstance(sender.get("id"), int):
                return sender["id"]
    return ("unknown", update["update_id"])


async def process_updates(
    bot: ValeriaBot, updates: list[dict[str, Any]], *, workers: int = 8
) -> bool:
    """Keep each user's updates ordered; let independent users make progress."""
    groups: dict[object, list[dict[str, Any]]] = defaultdict(list)
    for update in sorted(updates, key=lambda item: item["update_id"]):
        groups[_sender(update)].append(update)
    semaphore = asyncio.Semaphore(workers)

    async def process_group(group: list[dict[str, Any]]) -> None:
        async with semaphore:
            for update in group:
                await bot.handle_update(update)

    results = await asyncio.gather(
        *(process_group(group) for group in groups.values()), return_exceptions=True
    )
    successful = True
    for result in results:
        if isinstance(result, BaseException):
            # Exception messages and tracebacks can contain credentials and private prompts.
            logger.warning("Update handling failed (%s)", type(result).__name__)
            successful = False
    return successful


async def _poll_updates(
    bot: ValeriaBot,
    store: Store,
    telegram: TelegramClient,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        retry_delay = settings.poll_interval_seconds
        try:
            updates = await telegram.get_updates(store.get_offset(), settings.long_poll_seconds)
            if updates:
                if await process_updates(bot, updates):
                    store.set_offset(max(update["update_id"] for update in updates) + 1)
                else:
                    await _pause(stop_event, retry_delay)
        except Exception as error:
            logger.warning("Telegram polling failed (%s)", type(error).__name__)
            if isinstance(error, TelegramError) and error.retry_after is not None:
                retry_delay = max(retry_delay, min(error.retry_after, 300))
            await _pause(stop_event, retry_delay)


async def _poll_notifications(
    bot: ValeriaBot, settings: Settings, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        try:
            await bot.poll_notifications()
        except Exception as error:
            logger.warning("Notification polling failed (%s)", type(error).__name__)
        await _pause(stop_event, settings.poll_interval_seconds)


async def run_bot(
    bot: ValeriaBot,
    store: Store,
    telegram: TelegramClient,
    settings: Settings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop_event = stop_event if stop_event is not None else asyncio.Event()
    tasks = [
        asyncio.create_task(_poll_updates(bot, store, telegram, settings, stop_event)),
        asyncio.create_task(_poll_notifications(bot, settings, stop_event)),
    ]
    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
