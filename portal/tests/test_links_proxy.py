"""The Links page's routes hand every action to the backend's admin
listener, naming the operator, which the backend records in the audit log."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.access import require_access
from app.config import Settings
from app.routes import links

pytestmark = pytest.mark.anyio

# Taken before the fixture swaps httpx.AsyncClient for the stand-in backend.
RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(422, json={"detail": "invalid target: the URL must use https"})

    monkeypatch.setattr(
        links.httpx,
        "AsyncClient",
        lambda **kw: RealAsyncClient(transport=httpx.MockTransport(answer), **kw),
    )
    return sent


async def _call(method: str, path: str, **kwargs) -> httpx.Response:
    app = FastAPI()
    app.state.settings = Settings.model_construct(env="development", api_shared_secret="s3cret")
    app.middleware("http")(require_access)
    app.include_router(links.router)
    transport = httpx.ASGITransport(app=app)
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        return await c.request(
            method, path, headers={"Sec-Fetch-Site": "same-origin"}, **kwargs
        )


async def test_a_change_reaches_the_admin_listener_with_the_operator(backend) -> None:
    resp = await _call(
        "PATCH", "/api/links/ntust-calendar", json={"target_url": "http://x/"}
    )

    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid target: the URL must use https"}
    [sent] = backend
    assert sent.method == "PATCH"
    assert str(sent.url) == "http://tigerduck-internal-admin:9001/admin/links/ntust-calendar"
    assert sent.headers["X-Push-Token"] == "s3cret"
    assert sent.headers["X-Admin-Actor"] == "dev"
    assert sent.read() == b'{"target_url":"http://x/"}'


async def test_test_now_and_list_are_forwarded_too(backend) -> None:
    await _call("POST", "/api/links/ntust-calendar/test")
    await _call("GET", "/api/links")

    assert [(r.method, r.url.path) for r in backend] == [
        ("POST", "/admin/links/ntust-calendar/test"),
        ("GET", "/admin/links"),
    ]


async def test_a_name_that_is_not_a_link_name_is_not_forwarded(backend) -> None:
    resp = await _call("POST", "/api/links/%2E%2E/test")

    assert resp.status_code == 404
    assert backend == []
