"""The sync worker's request budget, on a virtual clock, against a fake
Moodle (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import heapq
import itertools

import httpx
import pytest

from server.syncjobs.budget import RequestBudget
from server.syncjobs.moodle_client import HttpAssignmentFetcher

pytestmark = pytest.mark.asyncio(loop_scope="session")


class VirtualClock:
    """Time moves only when every task waits on `sleep`: `run` then jumps
    to the earliest wake-up, so a test spends no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        wake = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + seconds, next(self._order), wake))
        await wake

    async def run(self, *aws):
        tasks = [asyncio.ensure_future(aw) for aw in aws]
        while not all(task.done() for task in tasks):
            # Nothing here waits on real I/O, so this many turns of the loop
            # bring every task to its next `sleep`.
            for _ in range(200):
                await asyncio.sleep(0)
            if all(task.done() for task in tasks):
                break
            assert self._timers, "a task waits on something other than the clock"
            wake_at, _, wake = heapq.heappop(self._timers)
            self.now = max(self.now, wake_at)
            wake.set_result(None)
        return [task.result() for task in tasks]


def _moodle(clock: VirtualClock, sent: list[float]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        if request.url.params["wsfunction"] == "core_webservice_get_site_info":
            return httpx.Response(200, json={"userid": 42, "fullname": "Test"})
        return httpx.Response(200, json={"lastattempt": {"submission": {"status": "new"}}})

    return httpx.MockTransport(answer)


async def test_jobs_in_flight_share_one_evenly_spaced_budget():
    """tigerduck-backend#80: two jobs at once must not reach the school
    faster than the budget, however many requests each makes."""
    clock = VirtualClock()
    sent: list[float] = []
    budget = RequestBudget(10, clock=clock, sleep=clock.sleep)
    fetcher = HttpAssignmentFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=_moodle(clock, sent),
        event_hooks=budget.event_hooks,
    )

    def probe():
        return fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=4
        )

    results = await clock.run(probe(), probe())

    assert [len(result) for result in results] == [3, 3]
    assert sent == [0.0, 6.0, 12.0, 18.0, 24.0, 30.0, 36.0, 42.0]


async def test_requests_woken_together_after_a_stall_still_go_spaced():
    """An event loop stalled past several turns wakes their waiters at
    once; requests that each held a turn then left together."""
    clock = VirtualClock()
    budget = RequestBudget(10, clock=clock, sleep=clock.sleep)
    went: list[float] = []

    async def request():
        await budget.acquire()
        went.append(clock.now)

    async def scenario():
        waiting = [asyncio.ensure_future(request()) for _ in range(4)]
        await asyncio.sleep(0)
        clock.now = 30.0
        await asyncio.gather(*waiting)

    await clock.run(scenario())

    assert went == [0.0, 30.0, 36.0, 42.0]


async def test_a_pause_holds_back_requests_already_waiting_and_new_ones():
    clock = VirtualClock()
    budget = RequestBudget(60, clock=clock, sleep=clock.sleep)
    went: list[float] = []

    async def request():
        await budget.acquire()
        went.append(clock.now)

    async def scenario():
        await request()
        waiting = asyncio.ensure_future(request())
        await asyncio.sleep(0)
        budget.pause(300)
        assert budget.paused
        await asyncio.gather(waiting, request(), request())

    await clock.run(scenario())

    assert went == [0.0, 300.0, 301.0, 302.0]
    assert not budget.paused
