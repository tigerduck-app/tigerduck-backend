"""Tests for POST /v3/devices/anonymous.

The endpoint is what puts a device with no account into the inventory
`custom_push_targeting` resolves against, so the things worth pinning down
are the ones targeting reads: which token column the platform lands in, and
whether the owner's push opt-out survives.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.rate_limit import SlidingWindowLimiter
from server.db import build_session_factory
from server.models import DeviceRegistration

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def fresh_limiters(client):
    """The limiters live on `app.state` for the life of the app, so without
    this each test would inherit whatever the previous ones spent — and the
    flood test, which swaps in a tighter cap, would leave it behind."""
    device, ip = (
        client.app.state.anon_device_limiter,
        client.app.state.anon_ip_limiter,
    )
    client.app.state.anon_device_limiter = SlidingWindowLimiter(
        max_attempts=100, window_seconds=60
    )
    client.app.state.anon_ip_limiter = SlidingWindowLimiter(
        max_attempts=1000, window_seconds=60
    )
    yield
    client.app.state.anon_device_limiter = device
    client.app.state.anon_ip_limiter = ip


def body(device_id: str, **overrides) -> dict:
    payload = {
        "device_id": device_id,
        "platform": "android",
        "device_class": "android",
        "push_token": "fcm-token-" + device_id,
        "bundle_id": "org.ntust.app.tigerduck",
    }
    payload.update(overrides)
    return payload


async def row_for(client, device_id: str) -> DeviceRegistration:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(DeviceRegistration).where(
                    DeviceRegistration.device_id == device_id
                )
            )
        ).scalar_one()


async def test_android_token_lands_in_pts_column(client) -> None:
    """Targeting gates Android on pts_token_hex and Apple on
    device_token_hex. A token in the wrong column reads as "no token" and
    the device silently drops out of every send."""
    response = await client.post(
        "/v3/devices/anonymous", json=body("android-token-col")
    )
    assert response.status_code == 200

    row = await row_for(client, "android-token-col")
    assert row.pts_token_hex == "fcm-token-android-token-col"
    assert row.device_token_hex is None


async def test_apple_token_lands_in_device_token_column(client) -> None:
    response = await client.post(
        "/v3/devices/anonymous",
        json=body("apple-token-col", platform="apple", device_class="iphone"),
    )
    assert response.status_code == 200

    row = await row_for(client, "apple-token-col")
    assert row.device_token_hex == "fcm-token-apple-token-col"
    assert row.pts_token_hex == ""


async def test_opt_out_is_recorded_on_create(client) -> None:
    """Signed out there is no session for the preferences PATCH, so the
    announce is the only way the opt-out reaches the row targeting reads."""
    response = await client.post(
        "/v3/devices/anonymous",
        json=body("opt-out-on-create", server_push_enabled=False),
    )
    assert response.status_code == 200

    row = await row_for(client, "opt-out-on-create")
    assert row.server_push_enabled is False


async def test_opt_out_survives_a_later_announce_that_omits_it(client) -> None:
    """An absent field means "nothing to report", not "back to the
    default" — otherwise the next launch would silently opt the user
    back in."""
    await client.post(
        "/v3/devices/anonymous",
        json=body("opt-out-sticky", server_push_enabled=False),
    )
    payload = body("opt-out-sticky")
    payload.pop("server_push_enabled", None)
    response = await client.post("/v3/devices/anonymous", json=payload)
    assert response.status_code == 200

    row = await row_for(client, "opt-out-sticky")
    assert row.server_push_enabled is False


async def test_opt_back_in_is_honoured(client) -> None:
    """The app has to be able to walk the opt-out back while still signed
    out, or a user who changed their mind before ever signing in would be
    stuck."""
    await client.post(
        "/v3/devices/anonymous",
        json=body("opt-back-in", server_push_enabled=False),
    )
    response = await client.post(
        "/v3/devices/anonymous",
        json=body("opt-back-in", server_push_enabled=True),
    )
    assert response.status_code == 200

    row = await row_for(client, "opt-back-in")
    assert row.server_push_enabled is True


async def test_absent_token_does_not_clear_the_stored_one(client) -> None:
    """The app announces on every launch and FCM only produces a token
    once it is ready; a tokenless announce must not un-reach the device."""
    await client.post("/v3/devices/anonymous", json=body("token-keeper"))
    payload = body("token-keeper", push_token=None)
    response = await client.post("/v3/devices/anonymous", json=payload)
    assert response.status_code == 200

    row = await row_for(client, "token-keeper")
    assert row.pts_token_hex == "fcm-token-token-keeper"


async def test_per_device_flood_is_rejected(client) -> None:
    # Fresh limiters so the assertion is about this test's calls and not
    # whatever the rest of the session already spent.
    client.app.state.anon_device_limiter = SlidingWindowLimiter(
        max_attempts=3, window_seconds=60
    )
    client.app.state.anon_ip_limiter = SlidingWindowLimiter(
        max_attempts=1000, window_seconds=60
    )
    for _ in range(3):
        ok = await client.post("/v3/devices/anonymous", json=body("flooder-01"))
        assert ok.status_code == 200

    blocked = await client.post("/v3/devices/anonymous", json=body("flooder-01"))
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "too_many_registration_attempts"

    # A different device is unaffected — the cap is per device, not global.
    other = await client.post("/v3/devices/anonymous", json=body("bystander"))
    assert other.status_code == 200


async def test_device_class_must_fit_the_platform(client) -> None:
    """Targeting matches on device_class alone once one is stored; an
    Android phone announcing itself as an iPhone would land in the iPhone
    audience and drop out of the Android one."""
    liar = await client.post(
        "/v3/devices/anonymous",
        json=body("android-liar", platform="android", device_class="iphone"),
    )
    assert liar.status_code == 422
    honest = await client.post(
        "/v3/devices/anonymous",
        json=body("apple-honest", platform="apple", device_class="mac"),
    )
    assert honest.status_code == 200
