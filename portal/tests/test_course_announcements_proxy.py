"""Re-extract on the Course announcements page goes to the backend's admin
listener, which queues the extraction and records the operator."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.access import require_access
from app.config import Settings
from app.routes import course_announcements

pytestmark = pytest.mark.anyio

# Taken before the fixture swaps httpx.AsyncClient for the stand-in backend.
RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            409, json={"detail": "an extraction of this version is already waiting"}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: RealAsyncClient(transport=httpx.MockTransport(answer), **kw),
    )
    return sent


async def _reextract(announcement_id: str) -> httpx.Response:
    app = FastAPI()
    app.state.settings = Settings.model_construct(env="development", api_shared_secret="s3cret")
    app.middleware("http")(require_access)
    app.include_router(course_announcements.router)
    transport = httpx.ASGITransport(app=app)
    async with RealAsyncClient(transport=transport, base_url="http://portal") as c:
        return await c.post(
            f"/api/course-announcements/announcements/{announcement_id}/reextract",
            headers={"Sec-Fetch-Site": "same-origin"},
        )


async def test_reextract_reaches_the_admin_listener_with_the_operator(backend) -> None:
    resp = await _reextract("101-901")

    assert resp.status_code == 409
    assert resp.json() == {"detail": "an extraction of this version is already waiting"}
    [sent] = backend
    assert sent.method == "POST"
    assert str(sent.url) == (
        "http://tigerduck-internal-admin:9001/admin/course-announcements/101-901/extract"
    )
    assert sent.headers["X-Push-Token"] == "s3cret"
    assert sent.headers["X-Admin-Actor"] == "dev"


async def test_an_id_that_is_not_an_announcement_id_is_not_forwarded(backend) -> None:
    resp = await _reextract("%2E%2E")

    assert resp.status_code == 404
    assert backend == []
