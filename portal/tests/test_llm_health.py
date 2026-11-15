"""The status page's LLM check sends the key the backend uses, and the key
never reaches the page or the logs."""
from __future__ import annotations

import logging

import httpx
import pytest
from app import status as checks
from app.config import Settings
from app.routes import status
from fastapi import FastAPI
from httpx import AsyncClient as RealAsyncClient

pytestmark = pytest.mark.anyio

KEY = "llm-key-x"


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        ok = request.headers.get("Authorization") == f"Bearer {KEY}"
        return httpx.Response(200 if ok else 401, json={"data": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        checks.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(answer), **kw),
    )

    async def nothing(*_args) -> dict:
        return {}

    async def no_containers() -> list:
        return []

    monkeypatch.setattr(status, "postgres_health", nothing)
    monkeypatch.setattr(status, "backend_version", nothing)
    monkeypatch.setattr(status, "docker_containers", no_containers)
    return sent


def test_the_key_is_read_from_the_backends_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIGERDUCK_LLM_API_KEY", KEY)

    assert Settings.from_env().llm_api_key == KEY


async def test_the_check_carries_the_key_and_the_page_never_shows_it(
    llm, caplog: pytest.LogCaptureFixture
) -> None:
    app = FastAPI()
    app.state.settings = Settings.model_construct(
        env="development", llm_base_url="http://llm:40001/v1", llm_api_key=KEY
    )
    app.include_router(status.router)
    caplog.set_level(logging.DEBUG)

    transport = httpx.ASGITransport(app=app)
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        page = await c.get("/api/status")
        env = await c.get("/api/env")

    assert str(llm[0].url) == "http://llm:40001/v1/models"
    assert llm[0].headers["Authorization"] == f"Bearer {KEY}"
    assert page.json()["llm"]["reachable"] is True
    assert KEY not in page.text
    assert KEY not in env.text
    assert KEY not in caplog.text
