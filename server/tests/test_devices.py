"""Device registration endpoint tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _register_payload(**overrides) -> dict:
    base = {
        "user_id": "user-abc",
        "device_id": "device-xyz",
        "pts_token_hex": "a1b2c3" * 10,
        "bundle_id": "org.ntust.app.TigerDuck",
        "attrs_type": "TigerDuckActivityAttributes",
        "apns_env": "development",
    }
    base.update(overrides)
    return base


async def test_register_device_creates_row(client: AsyncClient):
    payload = await _register_payload()
    response = await client.post("/v2/devices/register", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == "device-xyz"
    assert body["user_id"] == "user-abc"
    assert "registered_at" in body


async def test_register_is_idempotent_upsert(client: AsyncClient):
    p1 = await _register_payload(pts_token_hex="aa" * 32)
    p2 = await _register_payload(pts_token_hex="bb" * 32)

    r1 = await client.post("/v2/devices/register", json=p1)
    assert r1.status_code == 200

    r2 = await client.post("/v2/devices/register", json=p2)
    assert r2.status_code == 200

    got = await client.get("/v2/devices/device-xyz")
    assert got.status_code == 200


async def test_unregister_device_cascade(client: AsyncClient):
    payload = await _register_payload()
    await client.post("/v2/devices/register", json=payload)

    unreg = await client.post(
        "/v2/devices/unregister",
        json={"device_id": "device-xyz"},
    )
    assert unreg.status_code == 204

    got = await client.get("/v2/devices/device-xyz")
    assert got.status_code == 404


async def test_register_rejects_invalid_apns_env(client: AsyncClient):
    payload = await _register_payload(apns_env="staging")
    response = await client.post("/v2/devices/register", json=payload)
    assert response.status_code == 422


async def test_register_persists_device_class_and_opt_in(client: AsyncClient):
    payload = await _register_payload(
        device_id="iphone-d1",
        device_class="iphone",
        server_push_enabled=True,
    )
    resp = await client.post("/v2/devices/register", json=payload)
    assert resp.status_code == 200, resp.text


async def test_patch_preferences_flips_server_push_enabled(client: AsyncClient):
    p = await _register_payload(device_id="iphone-d2", device_class="iphone")
    await client.post("/v2/devices/register", json=p)
    resp = await client.patch(
        "/v2/devices/iphone-d2/preferences",
        json={"server_push_enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "device_id": "iphone-d2",
        "server_push_enabled": False,
    }


async def test_patch_preferences_404_for_unknown_device(client: AsyncClient):
    resp = await client.patch(
        "/v2/devices/does-not-exist/preferences",
        json={"server_push_enabled": False},
    )
    assert resp.status_code == 404


async def test_list_devices_returns_registered_rows(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="list-a", user_id="u1"),
    )
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="list-b", user_id="u2"),
    )

    resp = await client.get("/v2/devices")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 2
    ids = {item["device_id"] for item in body["items"]}
    assert {"list-a", "list-b"}.issubset(ids)
    sample = next(i for i in body["items"] if i["device_id"] == "list-a")
    # Tokens are reported as presence booleans only — the raw hex never
    # leaves the backend.
    assert sample["has_pts_token"] is True
    assert "pts_token_hex" not in sample
