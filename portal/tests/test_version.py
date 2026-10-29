"""`GET /api/version`: the backend's version for the sidebar."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.routes import status

pytestmark = pytest.mark.anyio


async def _get_version() -> httpx.Response:
    app = FastAPI()
    app.include_router(status.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as client:
        return await client.get("/api/version")


async def test_returns_what_the_backend_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reported() -> dict:
        return {"ok": True, "version": "3.7.0", "api_base_path": "/v3"}

    monkeypatch.setattr(status, "backend_version", reported)

    r = await _get_version()

    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "3.7.0", "api_base_path": "/v3"}


async def test_reports_an_unreachable_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable() -> dict:
        return {"ok": False, "detail": "ConnectError: refused"}

    monkeypatch.setattr(status, "backend_version", unreachable)

    r = await _get_version()

    assert r.status_code == 200
    assert r.json() == {"ok": False, "detail": "ConnectError: refused"}
