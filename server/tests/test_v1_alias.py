"""Retired /v1 and /v2 prefixes answer 410 Gone; unversioned probes survive."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")

SUNSET_BODY = {
    "detail": (
        "This API version has been retired. Update the app to the latest version."
    ),
    "current_api": "/v3",
}


@pytest.mark.parametrize(
    "path",
    [
        "/v1",
        "/v2",
        "/v1/ping",  # existed as the deprecated alias before the sunset
        "/v2/devices/register",  # existed as a real v2 route before the sunset
        "/v1/definitely/not/a/route",  # arbitrary path — prefix alone triggers 410
    ],
)
async def test_legacy_get_returns_410_gone(client: AsyncClient, path: str):
    response = await client.get(path)
    assert response.status_code == 410
    assert response.json() == SUNSET_BODY


async def test_legacy_post_returns_410_gone(client: AsyncClient):
    # The sunset applies to every method, not just GET: the old v2 register
    # POST gets the same 410 body instead of a 404/405.
    payload = {
        "user_id": "legacy-user",
        "device_id": "legacy-device",
        "pts_token_hex": "ab" * 32,
        "bundle_id": "org.ntust.app.TigerDuck",
        "attrs_type": "TigerDuckActivityAttributes",
        "apns_env": "development",
    }
    response = await client.post("/v2/devices/register", json=payload)
    assert response.status_code == 410
    assert response.json() == SUNSET_BODY


async def test_legacy_responses_carry_no_deprecation_alias_headers(
    client: AsyncClient,
):
    # The old v1→v2 alias decorated responses with Deprecation/Link headers.
    # That mechanism is gone; the sunset answer is the plain 410 payload.
    response = await client.get("/v1/ping")
    assert response.status_code == 410
    assert "Deprecation" not in response.headers
    assert "Link" not in response.headers


async def test_unversioned_ping_survives_sunset(client: AsyncClient):
    response = await client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": "tigerduck"}


async def test_unversioned_health_survives_sunset(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "development"}


async def test_unversioned_version_survives_sunset_and_points_at_v3(
    client: AsyncClient,
):
    response = await client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert body["api_base_path"] == "/v3"
    assert body["version"]
