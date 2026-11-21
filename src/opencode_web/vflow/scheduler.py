from __future__ import annotations

import asyncio
import contextlib
import logging

from .runtime import VFlowRuntime

logger = logging.getLogger(__name__)


class VFlowScheduler:
    """Background loop that advances VFlow runs without blocking the event loop."""

    def __init__(
        self,
        runtime: VFlowRuntime,
        *,
        limit: int = 20,
        idle_interval: float = 0.5,
    ) -> None:
        self.runtime = runtime
        self.limit = limit
        self.idle_interval = idle_interval
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="vflow-scheduler")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def wake(self) -> None:
        """Ask the loop to tick immediately instead of waiting for the idle poll.

        Event delivery is only a latency optimization; the database remains the
        source of truth.  This must be called from the scheduler's event loop
        thread (as ``EventBroker.publish`` callbacks are).
        """
        self._wake.set()

    async def run_once(self) -> int:
        return await asyncio.to_thread(self.runtime.tick, self.limit)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
                if processed == 0:
                    await self._wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("vflow scheduler iteration failed")
                await self._wait()
            # The tick we just ran consumes any pending wake.  Clearing after
            # the wait keeps a wake raised while the worker thread was busy.
            self._wake.clear()

    async def _wait(self) -> None:
        """Wait for a wake signal, stop, or the idle poll interval."""
        if self._stop.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_interval)
