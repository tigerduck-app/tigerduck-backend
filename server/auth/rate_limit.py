"""In-memory sliding-window rate limiter for login attempts.

Per-process only — fine for the current single-instance Docker Compose
deployment. If the backend ever scales to multiple replicas, move the
counters to Postgres or Redis; the call sites in `server/auth/service.py`
only touch this class, so the swap is local.

Why this exists: the login endpoint verifies the
submitted Moodle token by calling Moodle, which makes unthrottled logins
both a credential-validation oracle and a way to get our single server IP
rate-limited by the school.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def _prune(self, key: str) -> deque[float]:
        cutoff = self._clock() - self._window
        bucket = self._attempts.get(key)
        if bucket is None:
            bucket = deque()
            self._attempts[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            # Don't let abandoned keys accumulate forever.
            self._attempts.pop(key, None)
        return bucket

    def allow(self, key: str) -> bool:
        return len(self._prune(key)) < self._max_attempts

    def record(self, key: str) -> None:
        bucket = self._attempts.setdefault(key, deque())
        bucket.append(self._clock())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)
