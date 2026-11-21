from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from threading import Lock


class VFlowRateLimiter:
    """Small process-local sliding-window limiter for pipeline launches."""

    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, user_id: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events.setdefault(user_id, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def retry_after(self, user_id: str) -> int:
        with self._lock:
            events = self._events.get(user_id)
            if not events:
                return 0
            wait = events[0] + self.window_seconds - self._clock()
        return max(0, math.ceil(wait))
