"""/api/env tells the SPA whether the request came through Cloudflare Access.

The layout shows the signed-in email and a sign-out link only when it did.
A request that reached the portal directly (dev, LAN) has no Access session
to end, so the link would do nothing there.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.routes import status

pytestmark = pytest.mark.anyio

ACCESS_EMAIL = "Cf-Access-Authenticated-User-Email"


async def _get_env(headers: dict[str, str] | None = None) -> httpx.Response:
    # The route only reads app.state.settings, so skip the lifespan and
    # its database pool and set the settings directly. model_construct
    # keeps the field defaults and reads nothing from the shell's env.
    app = FastAPI()
    app.state.settings = Settings.model_construct()
    app.include_router(status.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as client:
        return await client.get("/api/env", headers=headers)


async def test_env_reports_the_access_user_and_where_to_sign_out() -> None:
    r = await _get_env({ACCESS_EMAIL: "ops@example.com"})

    assert r.status_code == 200
    assert r.json()["access"] == {
        "email": "ops@example.com",
        "logout_url": "/cdn-cgi/access/logout",
    }


async def test_env_reports_no_access_session_for_a_direct_request() -> None:
    r = await _get_env()

    assert r.status_code == 200
    assert r.json()["access"] is None


async def test_env_treats_a_blank_access_header_as_no_session() -> None:
    r = await _get_env({ACCESS_EMAIL: "   "})

    assert r.status_code == 200
    assert r.json()["access"] is None


async def test_env_is_never_cached() -> None:
    # The body now names the signed-in operator, and a direct request gets
    # access: null. A shared cache in front of the portal must not hand
    # either answer to someone else.
    r = await _get_env({ACCESS_EMAIL: "ops@example.com"})

    assert r.headers["cache-control"] == "private, no-store"
