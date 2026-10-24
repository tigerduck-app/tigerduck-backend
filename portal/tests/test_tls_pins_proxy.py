"""The portal's pin actions reach the backend's admin listener with the
shared secret and the operator, and pass its answer back."""
from __future__ import annotations

import json

import httpx
import pytest
from app.access import require_access
from app.config import Settings
from app.routes import tls_pins
from fastapi import FastAPI
from httpx import AsyncClient as RealAsyncClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/remove"):
            return httpx.Response(
                422, json={"detail": [{"msg": "Field required"}, {"msg": "Bad host"}]}
            )
        return httpx.Response(200, json={"ok": True, "serial": 1793001600})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        tls_pins.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(answer), **kw),
    )
    return sent


def _portal() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings.model_construct(env="development", api_shared_secret="s3cret")
    app.middleware("http")(require_access)
    app.include_router(tls_pins.router)
    return app


async def test_an_action_carries_the_secret_the_operator_and_the_body(backend) -> None:
    transport = httpx.ASGITransport(app=_portal())
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        r = await c.post(
            "/api/tls-pins/sets/ntust/pins",
            json={"spki": "x", "label": "y"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )

    assert r.status_code == 200 and r.json()["serial"] == 1793001600
    sent = backend[0]
    assert str(sent.url) == "http://tigerduck-internal-admin:9001/tls-pins/sets/ntust/pins"
    assert sent.headers["X-Push-Token"] == "s3cret"
    assert sent.headers["X-Portal-Actor"] == "dev"
    assert json.loads(sent.content) == {"spki": "x", "label": "y"}


async def test_publish_now_sends_the_pending_pins_it_showed(backend) -> None:
    transport = httpx.ASGITransport(app=_portal())
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        r = await c.post(
            "/api/tls-pins/publish",
            json={"approve": {"ntust": ["x"]}},
            headers={"Sec-Fetch-Site": "same-origin"},
        )

    assert r.status_code == 200
    assert json.loads(backend[0].content) == {"approve": {"ntust": ["x"]}}


async def test_a_validation_error_comes_back_as_one_message(backend) -> None:
    transport = httpx.ASGITransport(app=_portal())
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        r = await c.post(
            "/api/tls-pins/sets/ntust/pins/remove",
            json={},
            headers={"Sec-Fetch-Site": "same-origin"},
        )

    assert r.status_code == 422
    assert r.json() == {"detail": "Field required; Bad host"}
