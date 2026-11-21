from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

EventListener = Callable[[dict[str, Any]], None]


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._listeners: set[EventListener] = set()

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        """Register a synchronous listener and return its unsubscribe callback.

        Listeners run in the event loop as part of ``publish`` and must not
        block.  A failing listener is logged and never prevents delivery to
        SSE subscribers or other listeners.
        """
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def publish(self, event: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("event listener failed")

    async def subscribe(self) -> AsyncIterator[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield None
        finally:
            self._subscribers.discard(queue)
