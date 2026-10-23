"""`GET /ready`: the compose healthcheck's database and scheduler check."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from server import main as main_module

pytestmark = pytest.mark.asyncio(loop_scope="session")

READY_METRIC = "tigerduck_ready_last_ok_timestamp_seconds"


class _HangingEngine:
    @asynccontextmanager
    async def connect(self):
        await asyncio.sleep(10)
        yield


async def test_ready_when_the_database_answers_and_the_scheduler_runs(client):
    client.app.state.scheduler = SimpleNamespace(running=True)
    before = REGISTRY.get_sample_value(READY_METRIC)

    resp = await client.get("/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert REGISTRY.get_sample_value(READY_METRIC) > before


async def test_not_ready_when_the_scheduler_is_stopped(client):
    client.app.state.scheduler = SimpleNamespace(running=False)

    resp = await client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"status": "fail"}


async def test_not_ready_when_the_database_does_not_answer_in_time(
    client, monkeypatch
):
    client.app.state.scheduler = SimpleNamespace(running=True)
    client.app.state.engine = _HangingEngine()
    monkeypatch.setattr(main_module, "_READY_DB_TIMEOUT_SECONDS", 0.05)
    before = REGISTRY.get_sample_value(READY_METRIC)

    resp = await asyncio.wait_for(client.get("/ready"), timeout=2)

    assert resp.status_code == 503
    assert resp.json() == {"status": "fail"}
    assert REGISTRY.get_sample_value(READY_METRIC) == before

