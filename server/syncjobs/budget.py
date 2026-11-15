"""A pace toward Moodle (tigerduck-backend#80).

Every job in flight sends its Moodle requests through one `RequestBudget`,
so the school sees the same rate however many jobs run at once, and one
HTTP 429 holds all of them back. The sync worker and the course
announcement poll each have one, counted on a metric of its own.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import httpx


class BudgetPaused(Exception):
    """A budget that shares the request's Moodle limit is paused."""


class RequestBudget:
    """Lets a request go at most `per_minute` times a minute, each at least
    `60 / per_minute` seconds after the one before, and none while paused.

    Pass `event_hooks` to the httpx clients whose requests it paces;
    `observe` counts each request as it goes. A request raises BudgetPaused,
    unsent, while a budget in `stopped_by` is paused.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        observe: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        stopped_by: Sequence[RequestBudget] = (),
    ) -> None:
        self._spacing = 60.0 / per_minute
        self._stopped_by = tuple(stopped_by)
        self._observe = observe
        self._clock = clock
        self._sleep = sleep
        self._last_at = float("-inf")
        self._resume_at = float("-inf")
        self.event_hooks = {"request": [self._before_request]}

    @property
    def paused(self) -> bool:
        return self._clock() < self._resume_at

    def pause(self, seconds: float) -> None:
        self._resume_at = max(self._resume_at, self._clock() + seconds)

    async def acquire(self) -> None:
        # Measured from when the last request went, and checked again on
        # waking: a stalled event loop wakes several waiters at once.
        while True:
            now = self._clock()
            ready_at = max(self._resume_at, self._last_at + self._spacing)
            if now >= ready_at:
                self._last_at = now
                return
            await self._sleep(ready_at - now)

    async def _before_request(self, _request: httpx.Request) -> None:
        await self.acquire()
        # After the wait: the other budget may have paused during it.
        if any(other.paused for other in self._stopped_by):
            raise BudgetPaused
        self._observe()
