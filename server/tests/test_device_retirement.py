"""Deregistering the devices that are gone (`server/auth/retirement.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from server.admin_audit import AdminAuditLog
from server.auth.models import AuthSession, DevicePushToken, User, UserDevice
from server.auth.retirement import SYSTEM_ACTOR, retire_dead_devices
from server.db import build_session_factory
from server.models import DeviceRegistration
from server.scheduler.runtime import build_scheduler
from server.tests.test_auth_login import LOGIN_BODY, allow_moodle

pytestmark = pytest.mark.asyncio(loop_scope="session")

DAY = timedelta(days=1)
NOW = datetime.now(UTC)
LIVE = {"issued_at": NOW - DAY, "expires_at": NOW + 89 * DAY}
EXPIRED = {"issued_at": NOW - 100 * DAY, "expires_at": NOW - 10 * DAY}
REVOKED = {
    "issued_at": NOW - 10 * DAY,
    "expires_at": NOW + 80 * DAY,
    "revoked_at": NOW - 9 * DAY,
    "revoked_reason": "admin_revoked",
}


async def _device(
    session,
    label: str,
    *,
    user: User | None = None,
    seen_ago: timedelta | None = 8 * DAY,
    sessions: tuple[dict, ...] = (),
    pushed_ago: timedelta | None = None,
    announced_ago: timedelta | None = None,
) -> UserDevice:
    """A device of `user` (one of its own by default) last seen `seen_ago`
    before now, holding `sessions`, an active standard token and a Live
    Activity token, an already dropped token, and an anonymous announce
    `announced_ago` before now (None for none)."""
    if user is None:
        user = User()
        session.add(user)
        await session.flush()
    device = UserDevice(
        user_id=user.id,
        client_device_id=label,
        platform="ios",
        last_seen_at=None if seen_ago is None else NOW - seen_ago,
    )
    session.add(device)
    await session.flush()
    for index, values in enumerate(sessions):
        session.add(
            AuthSession(
                user_id=user.id,
                device_id=device.id,
                refresh_token_hash=f"{label}-{index}",
                **values,
            )
        )
    for kind, status in (
        ("standard", "active"),
        ("push_to_start", "active"),
        ("standard", "invalidated"),
    ):
        session.add(
            DevicePushToken(
                device_id=device.id,
                provider="apns",
                token_kind=kind,
                token_hash=f"{label}-{kind}-{status}",
                token_value=f"tok-{label}",
                status=status,
                last_success_at=(
                    None if pushed_ago is None or kind != "standard" else NOW - pushed_ago
                ),
            )
        )
    if announced_ago is not None:
        session.add(
            DeviceRegistration(
                device_id=label,
                user_id=f"anon-{label}",
                pts_token_hex="",
                bundle_id="",
                attrs_type="",
                apns_env="",
                updated_at=NOW - announced_ago,
            )
        )
    await session.commit()
    return device


async def _token_statuses(session, device: UserDevice) -> list[tuple[int, str]]:
    return (
        await session.execute(
            select(DevicePushToken.id, DevicePushToken.status)
            .where(DevicePushToken.device_id == device.id)
            .order_by(DevicePushToken.id)
        )
    ).all()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"seen_ago": None},
        {"sessions": (EXPIRED,)},
        {"sessions": (REVOKED,)},
        {"sessions": (EXPIRED, REVOKED), "pushed_ago": 8 * DAY, "announced_ago": 8 * DAY},
    ],
    ids=["no-session", "never-seen", "expired-session", "revoked-session", "all-old"],
)
async def test_a_device_silent_for_a_week_without_a_live_session_is_deregistered(
    db_session, prepared_engine, fields
):
    device = await _device(db_session, "iphone-1", **fields)
    tokens = await _token_statuses(db_session, device)
    factory = build_session_factory(prepared_engine)

    assert await retire_dead_devices(factory, now=NOW) == 1

    await db_session.refresh(device)
    assert device.deleted_at == NOW
    assert [status for _, status in await _token_statuses(db_session, device)] == [
        "invalidated"
    ] * 3
    audit = (await db_session.execute(select(AdminAuditLog))).scalar_one()
    assert (
        audit.actor,
        audit.action,
        audit.target_user_id,
        audit.target_device_id,
        audit.detail,
    ) == (
        SYSTEM_ACTOR,
        "device.deregister",
        device.user_id,
        device.id,
        {"invalidated_token_ids": [token_id for token_id, _ in tokens[:2]]},
    )
    # Deregistered once: the next day's run finds nothing.
    assert await retire_dead_devices(factory, now=NOW + DAY) == 0
    assert len((await db_session.execute(select(AdminAuditLog))).all()) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"seen_ago": timedelta(0), "sessions": (LIVE,)},
        {"seen_ago": 30 * DAY, "sessions": (EXPIRED, LIVE)},
        {"seen_ago": 6 * DAY, "sessions": (EXPIRED,)},
        {"sessions": (REVOKED,), "announced_ago": 6 * DAY},
        {"sessions": (EXPIRED,), "pushed_ago": 6 * DAY},
    ],
    ids=[
        "signed-in-just-now-no-push",
        "live-session-unused-for-a-month",
        "seen-6-days-ago",
        "app-launched-6-days-ago",
        "push-taken-6-days-ago",
    ],
)
async def test_a_device_in_use_is_kept(db_session, prepared_engine, fields):
    """A live session can still bring the device back by itself; an announce
    or an accepted push says the app is still there, and the session reauth
    push (`server/push/session_reauth.py`) needs both the device and its
    token."""
    device = await _device(db_session, "iphone-1", **fields)

    assert await retire_dead_devices(build_session_factory(prepared_engine), now=NOW) == 0

    await db_session.refresh(device)
    assert device.deleted_at is None
    assert [status for _, status in await _token_statuses(db_session, device)] == [
        "active",
        "active",
        "invalidated",
    ]
    assert (await db_session.execute(select(AdminAuditLog))).first() is None


async def test_only_the_dead_device_of_a_user_is_deregistered(db_session, prepared_engine):
    fresh = await _device(db_session, "iphone-1", seen_ago=DAY, sessions=(LIVE,))
    user = await db_session.get(User, fresh.user_id)
    dead = await _device(db_session, "ipad-1", user=user, sessions=(EXPIRED,))

    assert await retire_dead_devices(build_session_factory(prepared_engine), now=NOW) == 1

    await db_session.refresh(fresh)
    await db_session.refresh(dead)
    assert (fresh.deleted_at, dead.deleted_at) == (None, NOW)


async def test_signing_in_brings_a_deregistered_device_back(client) -> None:
    allow_moodle(client)
    assert (await client.post("/v3/auth/login", json=LOGIN_BODY)).status_code == 200
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(update(UserDevice).values(last_seen_at=NOW - 8 * DAY))
        await session.execute(update(AuthSession).values(**EXPIRED))
        await session.commit()
    assert await retire_dead_devices(factory, now=NOW) == 1

    assert (await client.post("/v3/auth/login", json=LOGIN_BODY)).status_code == 200

    async with factory() as session:
        device = (await session.execute(select(UserDevice))).scalar_one()
        assert device.deleted_at is None
    assert await retire_dead_devices(factory) == 0


async def test_the_job_runs_at_four_in_the_morning_at_school(test_settings) -> None:
    """At a wall-clock time: an interval would start over at every deploy."""
    scheduler = build_scheduler(object(), object(), test_settings, llm=object())
    scheduler.start(paused=True)
    try:
        started = datetime.now(UTC)
        run_at = scheduler.get_job("device_retirement").next_run_time
    finally:
        scheduler.shutdown(wait=False)
    local = run_at.astimezone(ZoneInfo("Asia/Taipei"))
    assert (local.hour, local.minute, local.second) == (4, 0, 0)
    assert timedelta(0) < run_at - started <= DAY
