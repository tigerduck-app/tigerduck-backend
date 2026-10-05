"""`POST /push-tick`: the portal's "run the pipeline now".

A tick drains every due job, which after a large send outlasts the
portal's 30s wait on this call. The answer has to say what happened —
done, failed, or still sending — without cutting a long tick off.
"""

from __future__ import annotations

import asyncio

import pytest

from server import main as main_module
from server.push import pipeline as pipeline_module


@pytest.fixture
def worker(client):
    client.app.state.push_worker = object()
    return client.app.state.push_worker


async def test_a_tick_that_finishes_is_reported_done(client, worker, monkeypatch):
    async def tick(_worker):
        return 3

    monkeypatch.setattr(pipeline_module, "run_push_tick", tick)

    resp = await client.post("/push-tick")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "done": True, "processed": 3}


async def test_a_tick_that_fails_is_reported_failed(client, worker, monkeypatch):
    async def tick(_worker):
        raise RuntimeError("database went away")

    monkeypatch.setattr(pipeline_module, "run_push_tick", tick)

    resp = await client.post("/push-tick")

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert "database went away" in resp.json()["error"]


async def test_a_long_tick_is_reported_still_sending_and_carries_on(
    client, worker, monkeypatch
):
    finished = asyncio.Event()

    async def tick(_worker):
        await asyncio.sleep(0.3)
        finished.set()
        return 500

    monkeypatch.setattr(pipeline_module, "run_push_tick", tick)
    monkeypatch.setattr(main_module, "_PUSH_TICK_WAIT_SECONDS", 0.05)

    resp = await client.post("/push-tick")

    assert resp.json() == {"ok": True, "done": False}
    # Answered, not cancelled: the drain still finishes.
    await asyncio.wait_for(finished.wait(), timeout=2)


async def test_shutdown_lets_a_tick_still_sending_finish(monkeypatch):
    # Shutdown closes the push router and the database a tick sends
    # through; one cut off mid-delivery leaves its job in `processing`
    # until stale recovery, minutes later.
    finished = asyncio.Event()

    async def tick():
        await asyncio.sleep(0.2)
        finished.set()

    task = asyncio.create_task(tick())
    monkeypatch.setattr(main_module, "_running_ticks", {task})

    await main_module._finish_running_ticks(timeout=2)

    assert finished.is_set()


async def test_shutdown_waits_for_a_tick_only_so_long(monkeypatch):
    stuck = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(main_module, "_running_ticks", {stuck})

    await asyncio.wait_for(main_module._finish_running_ticks(timeout=0.05), timeout=1)

    stuck.cancel()
