"""A forced push tick that ran is reported as run even when its audit row
cannot be written."""
from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient as RealAsyncClient
from fastapi import FastAPI

from app.access import require_access
from app.config import Settings
from app.routes.moodle import push

pytestmark = pytest.mark.anyio


class _Backend:
    """Stands in for httpx.AsyncClient: the backend ran the tick."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_Backend":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url: str, headers: dict) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "done": True})


class _DownPool:
    def acquire(self):
        raise OSError("database unreachable")


async def test_a_tick_that_ran_answers_200_when_the_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(push.httpx, "AsyncClient", _Backend)
    app = FastAPI()
    app.state.settings = Settings.model_construct(env="development")
    app.state.pool = _DownPool()
    app.middleware("http")(require_access)
    app.include_router(push.router)

    transport = httpx.ASGITransport(app=app)
    # Imported before the patch: httpx.AsyncClient is the stand-in by now.
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        r = await c.post("/api/moodle/push-tick", headers={"Sec-Fetch-Site": "same-origin"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "done": True}
