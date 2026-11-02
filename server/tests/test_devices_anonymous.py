"""Tests for POST /v3/devices/anonymous.

The endpoint is what puts a device with no account into the inventory
`custom_push_targeting` resolves against, so the things worth pinning down
are the ones targeting reads: which token column the platform lands in, and
whether the owner's push opt-out survives.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
import structlog
from sqlalchemy import select, update
from structlog.testing import capture_logs

from server.auth.rate_limit import SlidingWindowLimiter
from server.db import build_session_factory
from server.models import DeviceRegistration
from server.routes import user_devices

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


def secret_for(seed: str) -> str:
    """A device secret as the apps mint one: 32 bytes, unpadded base64url."""
    digest = hashlib.sha256(seed.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


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


async def row_for(client, device_id: str) -> DeviceRegistration | None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(DeviceRegistration).where(
                    DeviceRegistration.device_id == device_id
                )
            )
        ).scalar_one_or_none()


SECRET = secret_for("this device")
OTHER = secret_for("another caller")
REFUSED = {"detail": "device_secret_invalid"}


def hashed(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def announce(client, device_id: str, **overrides):
    return await client.post("/v3/devices/anonymous", json=body(device_id, **overrides))


def columns(row: DeviceRegistration) -> dict:
    return {c.key: getattr(row, c.key) for c in DeviceRegistration.__table__.columns}


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


async def test_a_device_id_too_long_for_its_row_is_refused(client) -> None:
    """The row's `user_id` is "anon-" and the id, in 64 characters; a longer
    one failed at the database with a 500 (tigerduck-backend#93)."""
    longest = "d" * 59
    assert (await client.post("/v3/devices/anonymous", json=body(longest))).status_code == 200
    response = await client.post("/v3/devices/anonymous", json=body(longest + "d"))
    assert response.status_code == 422


async def test_a_malformed_secret_is_refused_and_never_echoed(client, capsys, caplog) -> None:
    short = "malformed-secret-20c"
    assert len(short) == 20
    with caplog.at_level("DEBUG"):
        response = await client.post(
            "/v3/devices/anonymous", json=body("short-secret", device_secret=short)
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "device_secret"]
    assert short not in response.text
    written = capsys.readouterr()
    assert short not in written.out + written.err
    assert short not in caplog.text
    assert await row_for(client, "short-secret") is None


# --- Device secrets ------------------------------------------------------------


async def test_an_updated_app_creates_its_row_with_the_hash_of_its_secret(client) -> None:
    response = await announce(client, "new-updated", device_secret=SECRET)

    assert response.status_code == 200
    row = await row_for(client, "new-updated")
    assert row.secret_hash == hashed(SECRET)
    assert SECRET not in response.text and row.secret_hash not in response.text


async def test_a_released_app_creates_its_row_with_no_secret(client) -> None:
    assert (await announce(client, "new-released")).status_code == 200
    assert (await row_for(client, "new-released")).secret_hash is None


async def test_the_matching_secret_changes_a_bound_row(client) -> None:
    await announce(client, "bound-match", device_secret=SECRET)
    response = await announce(
        client, "bound-match", device_secret=SECRET, push_token="fcm-rotated",
        server_push_enabled=False,
    )

    assert response.status_code == 200
    row = await row_for(client, "bound-match")
    assert (row.pts_token_hex, row.server_push_enabled) == ("fcm-rotated", False)
    assert row.secret_hash == hashed(SECRET)


async def test_a_bound_row_refuses_a_request_without_its_secret(client) -> None:
    await announce(client, "bound-missing", device_secret=SECRET)
    before = columns(await row_for(client, "bound-missing"))

    response = await announce(
        client, "bound-missing", push_token="fcm-other", server_push_enabled=False
    )

    assert (response.status_code, response.json()) == (403, REFUSED)
    after = columns(await row_for(client, "bound-missing"))
    assert after == before and after["updated_at"] == before["updated_at"]


async def test_a_bound_row_refuses_another_secret(client) -> None:
    await announce(client, "bound-other", device_secret=SECRET)
    before = columns(await row_for(client, "bound-other"))

    response = await announce(
        client, "bound-other", device_secret=OTHER, push_token="fcm-other"
    )

    assert (response.status_code, response.json()) == (403, REFUSED)
    assert columns(await row_for(client, "bound-other")) == before


async def test_the_real_device_claims_its_row_with_the_token_it_stores(client) -> None:
    await announce(client, "claim-real")

    response = await announce(
        client, "claim-real", device_secret=SECRET, server_push_enabled=False
    )

    assert response.status_code == 200
    row = await row_for(client, "claim-real")
    assert (row.secret_hash, row.server_push_enabled) == (hashed(SECRET), False)


async def test_a_caller_with_only_the_id_binds_nothing(client) -> None:
    await announce(client, "claim-foreign")
    before = columns(await row_for(client, "claim-foreign"))

    response = await announce(
        client, "claim-foreign", device_secret=OTHER, push_token="fcm-foreign"
    )

    assert (response.status_code, response.json()) == (403, REFUSED)
    after = columns(await row_for(client, "claim-foreign"))
    assert after == before
    assert (after["secret_hash"], after["pts_token_hex"]) == (None, "fcm-token-claim-foreign")


async def test_a_row_with_no_token_binds_the_secret(client) -> None:
    mac = {"platform": "apple", "device_class": "mac", "push_token": None}
    await announce(client, "claim-tokenless", **mac)

    response = await announce(client, "claim-tokenless", device_secret=SECRET, **mac)

    assert response.status_code == 200
    assert (await row_for(client, "claim-tokenless")).secret_hash == hashed(SECRET)


async def test_a_token_in_either_column_proves_the_claim(client) -> None:
    """A row from `/v2` can hold a token in both columns, and an Apple row
    whose device token was dropped still holds its push-to-start one."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            DeviceRegistration(
                device_id="claim-v2", user_id="anon-claim-v2", platform="apple",
                pts_token_hex="pts-from-v2", device_token_hex=None, bundle_id="b",
                attrs_type="", apns_env="",
            )
        )
        await session.commit()

    response = await announce(
        client, "claim-v2", platform="apple", device_class="iphone",
        push_token="pts-from-v2", device_secret=SECRET,
    )

    assert response.status_code == 200
    assert (await row_for(client, "claim-v2")).secret_hash == hashed(SECRET)


async def test_two_claims_at_once_bind_one_secret(client) -> None:
    mac = {"platform": "apple", "device_class": "mac", "push_token": None}
    await announce(client, "claim-race", **mac)

    answers = await asyncio.gather(
        announce(client, "claim-race", device_secret=SECRET, **mac),
        announce(client, "claim-race", device_secret=OTHER, **mac),
    )

    assert sorted(a.status_code for a in answers) == [200, 403]
    winner = SECRET if answers[0].status_code == 200 else OTHER
    assert (await row_for(client, "claim-race")).secret_hash == hashed(winner)


async def test_two_creates_at_once_bind_one_secret(client) -> None:
    """Both insert the same device_id; the second waits for the first and
    then finds its secret bound, where it used to fail with a 500."""
    answers = await asyncio.gather(
        announce(client, "create-race", device_secret=SECRET),
        announce(client, "create-race", device_secret=OTHER),
    )

    assert sorted(a.status_code for a in answers) == [200, 403]
    winner = SECRET if answers[0].status_code == 200 else OTHER
    assert (await row_for(client, "create-race")).secret_hash == hashed(winner)


async def test_a_secret_sent_before_the_token_binds_nothing(client) -> None:
    await announce(client, "claim-early")

    response = await announce(
        client, "claim-early", device_secret=SECRET, push_token=None,
        server_push_enabled=False,
    )

    assert response.status_code == 200
    row = await row_for(client, "claim-early")
    assert (row.secret_hash, row.server_push_enabled) == (None, False)
    assert row.pts_token_hex == "fcm-token-claim-early"


async def test_a_released_app_refreshes_its_row(client) -> None:
    await announce(client, "released-refresh")

    response = await announce(client, "released-refresh", server_push_enabled=False)

    assert response.status_code == 200
    assert (await row_for(client, "released-refresh")).server_push_enabled is False


async def test_a_request_without_a_secret_cannot_change_the_platform(client) -> None:
    await announce(client, "platform-flip", platform="apple", device_class="iphone")
    before = columns(await row_for(client, "platform-flip"))

    response = await announce(client, "platform-flip", platform="android", device_class="android")

    assert (response.status_code, response.json()) == (403, REFUSED)
    assert columns(await row_for(client, "platform-flip")) == before


async def test_a_request_without_a_secret_keeps_the_stored_token(client) -> None:
    await announce(client, "redirect")

    response = await announce(
        client, "redirect", push_token="fcm-elsewhere", device_class="android_tablet",
        server_push_enabled=False,
    )

    assert response.status_code == 200
    row = await row_for(client, "redirect")
    assert row.pts_token_hex == "fcm-token-redirect"
    assert (row.device_class, row.server_push_enabled) == ("android_tablet", False)


async def test_a_row_whose_token_was_dropped_takes_a_new_one(client) -> None:
    iphone = {"platform": "apple", "device_class": "iphone"}
    await announce(client, "dropped-token", **iphone)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        # As `_forget_dead_token` clears a token APNs refused.
        await session.execute(
            update(DeviceRegistration)
            .where(DeviceRegistration.device_id == "dropped-token")
            .values(device_token_hex=None)
        )
        await session.commit()

    response = await announce(client, "dropped-token", push_token="apns-new", **iphone)

    assert response.status_code == 200
    assert (await row_for(client, "dropped-token")).device_token_hex == "apns-new"


async def add_push_to_start_only_row(client, device_id: str) -> None:
    """An Apple row from `/v2` whose device token the dispatcher dropped."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            DeviceRegistration(
                device_id=device_id, user_id=f"anon-{device_id}", platform="apple",
                pts_token_hex="pts-from-v2", device_token_hex=None, bundle_id="b",
                attrs_type="", apns_env="",
            )
        )
        await session.commit()


async def test_a_released_app_restores_its_token_beside_a_push_to_start_one(client) -> None:
    await add_push_to_start_only_row(client, "pts-left")

    response = await announce(
        client, "pts-left", platform="apple", device_class="iphone", push_token="apns-new"
    )

    assert response.status_code == 200
    row = await row_for(client, "pts-left")
    assert (row.device_token_hex, row.pts_token_hex) == ("apns-new", "pts-from-v2")


async def test_an_updated_app_claims_a_row_with_only_a_push_to_start_token(client) -> None:
    await add_push_to_start_only_row(client, "pts-claim")

    response = await announce(
        client, "pts-claim", platform="apple", device_class="iphone",
        push_token="apns-new", device_secret=SECRET,
    )

    assert response.status_code == 200
    row = await row_for(client, "pts-claim")
    assert (row.secret_hash, row.device_token_hex) == (hashed(SECRET), "apns-new")


# --- The cut-off switch (TIGERDUCK_DEVICE_SECRET_REQUIRED) ---------------------


@pytest.fixture
def secret_required(client, monkeypatch):
    monkeypatch.setattr(client.app.state.settings, "device_secret_required", True)


async def test_the_switch_refuses_a_create_by_a_released_app(client, secret_required) -> None:
    response = await announce(client, "cutoff-create")

    assert (response.status_code, response.json()) == (403, REFUSED)
    assert await row_for(client, "cutoff-create") is None


async def test_the_switch_refuses_an_update_by_a_released_app(client, monkeypatch) -> None:
    await announce(client, "cutoff-update")
    before = columns(await row_for(client, "cutoff-update"))
    monkeypatch.setattr(client.app.state.settings, "device_secret_required", True)

    refused = [
        await announce(client, "cutoff-update", server_push_enabled=False),
        await announce(client, "cutoff-update", device_secret=SECRET, push_token=None),
    ]

    assert [(r.status_code, r.json()) for r in refused] == [(403, REFUSED)] * 2
    assert columns(await row_for(client, "cutoff-update")) == before


async def test_the_switch_still_lets_an_updated_app_claim_and_create(client, monkeypatch) -> None:
    await announce(client, "cutoff-claim")
    monkeypatch.setattr(client.app.state.settings, "device_secret_required", True)

    claim = await announce(client, "cutoff-claim", device_secret=SECRET)
    create = await announce(client, "cutoff-new", device_secret=OTHER)

    assert (claim.status_code, create.status_code) == (200, 200)
    assert (await row_for(client, "cutoff-claim")).secret_hash == hashed(SECRET)
    assert (await row_for(client, "cutoff-new")).secret_hash == hashed(OTHER)


async def test_binding_and_refusals_log_the_device_and_the_reason_only(
    client, monkeypatch
) -> None:
    # A cached logger keeps an earlier app's processors, out of capture_logs' reach.
    monkeypatch.setattr(user_devices, "logger", structlog.get_logger(user_devices.__name__))
    with capture_logs() as logs:
        await announce(client, "logged-create", device_secret=SECRET)
        await announce(client, "logged-claim")
        await announce(client, "logged-claim", device_secret=SECRET)
        await announce(client, "logged-create", device_secret=OTHER)
        await announce(client, "logged-create")
        await announce(client, "logged-claim", device_secret=OTHER, push_token="fcm-foreign")

    secret_logs = [log for log in logs if log["event"].startswith("device.secret_")]
    assert [(log["event"], log["device_id"], log["reason"]) for log in secret_logs] == [
        ("device.secret_bound", "logged-create", "created"),
        ("device.secret_bound", "logged-claim", "claimed"),
        ("device.secret_refused", "logged-create", "secret_mismatch"),
        ("device.secret_refused", "logged-create", "secret_missing"),
        ("device.secret_refused", "logged-claim", "secret_mismatch"),
    ]
    assert all(set(log) == {"event", "log_level", "device_id", "reason"} for log in secret_logs)
    written = repr(logs)
    for value in (SECRET, OTHER, hashed(SECRET), hashed(OTHER)):
        assert value not in written
