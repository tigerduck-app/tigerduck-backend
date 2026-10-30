"""The sync worker's pace toward Moodle (tigerduck-backend#80).

Every job in flight sends its Moodle requests through one `RequestBudget`,
so the school sees the same rate however many jobs run at once, and one
HTTP 429 holds all of them back.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from server.metrics import observe_sync_moodle_request


class RequestBudget:
    """Lets a request go at most `per_minute` times a minute, each at least
    `60 / per_minute` seconds after the one before, and none while paused.

    Pass `event_hooks` to the httpx clients whose requests it paces.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._spacing = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_at = float("-inf")
        self._resume_at = float("-inf")
        self.event_hooks = {"request": [self._before_request]}

    @property
    def paused(self) -> bool:
        return self._clock() < self._resume_at

    def pause(self, seconds: float) -> None:
        self._resume_at = max(self._resume_at, self._clock() + seconds)

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            if now < self._resume_at:
                await self._sleep(self._resume_at - now)
                continue
            slot = max(now, self._next_at)
            self._next_at = slot + self._spacing
            if slot <= now:
                return
            await self._sleep(slot - now)
            # A pause that began while this request waited for its turn
            # holds it too; it then takes a fresh turn after the pause.
            if not self.paused:
                return

    async def _before_request(self, _request: httpx.Request) -> None:
        await self.acquire()
        observe_sync_moodle_request()
