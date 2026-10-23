"""The portal's "run the pipeline now" reaches the backend's admin listener,
the only place `/push-tick` is served."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.routes.moodle import push

pytestmark = pytest.mark.anyio


async def test_force_push_tick_calls_the_admin_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[httpx.Request] = []

    def backend(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "done": True, "processed": 0})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(backend), **kw),
    )
    settings = Settings.model_construct(api_shared_secret="s3cret")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    resp = await push.force_push_tick(request)

    assert resp.status_code == 200
    assert str(sent[0].url) == "http://tigerduck-internal-admin:9001/push-tick"
    assert sent[0].headers["X-Push-Token"] == "s3cret"
