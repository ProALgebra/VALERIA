"""Cancellation and backoff shared by backend operations and queue polling."""

import asyncio
from collections.abc import Awaitable
from typing import Any

OPENCODE_RETRY_INITIAL_SECONDS = 1
OPENCODE_RETRY_MAX_SECONDS = 3 * 60 * 60


async def _await_or_cancel(
    awaitable: Awaitable[Any],
    cancel_event: asyncio.Event | None = None,
) -> Any:
    if cancel_event is None:
        return await awaitable
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel_event.wait())
    try:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        return await operation
    finally:
        operation.cancel()
        cancellation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


async def _wait_for_retry(delay: float, cancel_event: asyncio.Event | None = None) -> None:
    await _await_or_cancel(asyncio.sleep(delay), cancel_event)
