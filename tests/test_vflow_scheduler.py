from __future__ import annotations

import asyncio

import pytest

from opencode_web.app import create_app
from opencode_web.events import EventBroker
from opencode_web.vflow.scheduler import VFlowScheduler


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = 0
        self.limits: list[int] = []

    def tick(self, limit: int) -> int:
        self.calls += 1
        self.limits.append(limit)
        return 1 if self.calls == 1 else 0


class IdleRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def tick(self, limit: int) -> int:
        del limit
        self.calls += 1
        return 0


async def wait_for_ticks(runtime: IdleRuntime, count: int, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while runtime.calls < count:
        if loop.time() >= deadline:
            raise AssertionError(f"expected {count} ticks, got {runtime.calls}")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_scheduler_run_once_and_start_stop() -> None:
    runtime = FakeRuntime()
    scheduler = VFlowScheduler(runtime, limit=7, idle_interval=0.01)

    assert await scheduler.run_once() == 1
    assert runtime.calls == 1
    assert runtime.limits == [7]

    await scheduler.start()
    await asyncio.sleep(0.03)
    await scheduler.stop()
    assert runtime.calls >= 2


@pytest.mark.asyncio
async def test_wake_triggers_tick_without_waiting_for_idle_interval() -> None:
    runtime = IdleRuntime()
    scheduler = VFlowScheduler(runtime, idle_interval=30)

    await scheduler.start()
    try:
        await wait_for_ticks(runtime, 1)
        # The scheduler is now parked on the idle wait.
        await asyncio.sleep(0.02)
        assert runtime.calls == 1

        started = asyncio.get_running_loop().time()
        scheduler.wake()
        await wait_for_ticks(runtime, 2)
        assert asyncio.get_running_loop().time() - started < 1
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_start_stop_are_idempotent_and_wake_after_stop_is_safe() -> None:
    runtime = IdleRuntime()
    scheduler = VFlowScheduler(runtime, idle_interval=30)

    scheduler.wake()  # Before start: the wake is not lost.
    await scheduler.start()
    await scheduler.start()
    await wait_for_ticks(runtime, 2)
    await scheduler.stop()
    calls = runtime.calls

    await scheduler.stop()
    scheduler.wake()
    scheduler.wake()
    await asyncio.sleep(0.03)
    assert runtime.calls == calls


class FakeScheduler:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


@pytest.mark.asyncio
async def test_event_broker_job_updated_wakes_scheduler() -> None:
    broker = EventBroker()
    scheduler = FakeScheduler()

    def on_event(event: dict[str, object]) -> None:
        if event.get("type") == "job.updated":
            scheduler.wake()

    unsubscribe = broker.add_listener(on_event)
    await broker.publish({"type": "job.updated", "job": {"id": "job-1"}})
    await broker.publish({"type": "opencode.event"})
    assert scheduler.wakes == 1

    unsubscribe()
    await broker.publish({"type": "job.updated", "job": {"id": "job-2"}})
    assert scheduler.wakes == 1


@pytest.mark.asyncio
async def test_app_lifespan_wakes_scheduler_for_job_updated(settings, monkeypatch) -> None:
    class LifespanScheduler:
        def __init__(self, runtime) -> None:
            self.runtime = runtime
            self.wakes = 0
            self.stopped = False

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stopped = True

        def wake(self) -> None:
            self.wakes += 1

    monkeypatch.setattr("opencode_web.app.VFlowScheduler", LifespanScheduler)
    app = create_app(settings, start_dispatcher=False)

    async with app.router.lifespan_context(app):
        scheduler = app.state.vflow_scheduler
        await app.state.events.publish({"type": "job.updated", "job": {"id": "job-1"}})
        await app.state.events.publish({"type": "opencode.event"})
        assert scheduler.wakes == 1

    assert scheduler.stopped
    await app.state.events.publish({"type": "job.updated", "job": {"id": "job-2"}})
    assert scheduler.wakes == 1
