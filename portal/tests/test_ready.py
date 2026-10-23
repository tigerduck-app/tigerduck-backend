"""`GET /ready`: the compose healthcheck's check that the pool reaches postgres."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app.routes import status

pytestmark = pytest.mark.anyio


class _Pool:
    def __init__(self, answer=None, error: Exception | None = None, delay: float = 0) -> None:
        self.answer, self.error, self.delay = answer, error, delay

    async def fetchval(self, query: str):
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer


async def _get_ready(pool) -> httpx.Response:
    app = FastAPI()
    app.state.pool = pool
    app.include_router(status.liveness_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as client:
        return await client.get("/ready")


async def test_ready_when_the_pool_answers() -> None:
    r = await _get_ready(_Pool(answer=1))

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_not_ready_without_a_pool() -> None:
    r = await _get_ready(None)

    assert r.status_code == 503
    assert r.json() == {"status": "fail"}


async def test_not_ready_when_the_query_fails() -> None:
    r = await _get_ready(_Pool(error=OSError("connection refused")))

    assert r.status_code == 503
    assert r.json() == {"status": "fail"}


async def test_not_ready_when_the_query_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "_READY_DB_TIMEOUT_SECONDS", 0.05)

    r = await asyncio.wait_for(_get_ready(_Pool(answer=1, delay=10)), timeout=2)

    assert r.status_code == 503
    assert r.json() == {"status": "fail"}
