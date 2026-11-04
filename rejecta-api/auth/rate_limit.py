"""In-memory sliding-window rate limiters.

Good for a single API instance (current Railway setup). With several
instances each keeps its own window; move to Redis then, behind the same
interface. Persistent protection (account lockout) lives in the database.
"""

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` for each key."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit must be >= 1 and window_seconds > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _window(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            return deque()
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            del self._events[key]
        return events

    def retry_after(self, key: str) -> float:
        """Seconds until the next event is allowed (0 if allowed now)."""
        now = self._clock()
        events = self._window(key, now)
        if len(events) < self.limit:
            return 0.0
        return max(0.0, events[0] + self.window_seconds - now)

    def record(self, key: str) -> None:
        """Count one event for ``key``."""
        now = self._clock()
        events = self._window(key, now)
        if key not in self._events:
            if len(self._events) >= self.max_keys:
                self._prune(now)
            self._events[key] = events
        events.append(now)

    def hit(self, key: str) -> float:
        """Record an event if allowed. Returns 0 if allowed, else retry-after seconds."""
        wait = self.retry_after(key)
        if wait == 0.0:
            self.record(key)
        return wait

    def reset(self, key: str) -> None:
        self._events.pop(key, None)

    def clear(self) -> None:
        self._events.clear()

    def _prune(self, now: float) -> None:
        for key in list(self._events):
            self._window(key, now)
        if len(self._events) >= self.max_keys:
            # Still full: drop the oldest half rather than grow without bound.
            oldest = sorted(self._events, key=lambda k: self._events[k][-1])
            for key in oldest[: len(oldest) // 2]:
                del self._events[key]
