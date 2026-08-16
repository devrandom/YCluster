"""Sliding-window rate limiter for the `tail`/`status` polling tools.

Guards against an agent busy-polling a single backgrounded session (or the
unscoped log/listing) more than `max_calls` times per `window_s` seconds.
When a key hits the limit the call is refused and the caller is told to back
off for at least `recommended_sleep_s` before polling again. Refused calls do
not consume a slot, so a caller that keeps hitting the limit sees a stable
backoff recommendation until the window clears.
"""

import math
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_calls: int,
        window_s: float,
        *,
        recommended_sleep_s: float = 10.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError(f"max_calls must be >= 1, got {max_calls}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self.max_calls = max_calls
        self.window_s = window_s
        self.recommended_sleep_s = recommended_sleep_s
        self._now = now or time.monotonic
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record a call for `key` and return whether it's within the limit.

        Returns False (and records nothing) when the key already has
        `max_calls` hits inside the window.
        """
        now = self._now()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True

    def backoff_seconds(self, key: str) -> float:
        """Seconds until the oldest recorded hit for `key` ages out (0 if none)."""
        hits = self._hits.get(key)
        if not hits:
            return 0.0
        now = self._now()
        oldest = hits[0]
        return max(0.0, self.window_s - (now - oldest))
