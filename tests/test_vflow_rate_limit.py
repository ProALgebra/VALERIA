from __future__ import annotations

from opencode_web.vflow.rate_limit import VFlowRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_blocks_and_expires():
    clock = FakeClock()
    limiter = VFlowRateLimiter(limit=2, window_seconds=60, clock=clock)

    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is False
    assert limiter.retry_after("user-1") == 60
    assert limiter.allow("user-2") is True

    clock.now = 61
    assert limiter.allow("user-1") is True
    assert limiter.retry_after("user-1") == 60
    clock.now = 122
    assert limiter.retry_after("user-1") == 0
