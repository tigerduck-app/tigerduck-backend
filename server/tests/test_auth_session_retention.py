"""Pruning auth_sessions past their expiry, and the days kept for the
Userbase dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from server.auth.models import AuthSession, DeviceActiveDay, User, UserDevice
from server.auth.retention import prune_expired_sessions
from server.config import Settings
from server.db import build_session_factory
from server.tests.test_auth_refresh import _refresh, do_login

pytestmark = pytest.mark.asyncio(loop_scope="session")

SETTINGS = Settings(auth_session_retention_days=30)
NOW = datetime.now(UTC)
GONE, KEPT = NOW - timedelta(days=31), NOW - timedelta(days=29)
DAY = datetime(2026, 6, 1, tzinfo=UTC)


def _session(device: UserDevice, name: str, *, issued: datetime, expires: datetime, **more):
    return AuthSession(
        user_id=device.user_id,
        device_id=device.id,
        refresh_token_hash=name,
        issued_at=issued,
        created_at=issued,
        expires_at=expires,
        **more,
    )


async def test_sessions_long_expired_go_and_their_days_stay(
    db_session, prepared_engine
) -> None:
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(user_id=user.id, client_device_id="iphone-1", platform="ios")
    db_session.add(phone)
    await db_session.flush()
    rival = _session(phone, "rival", issued=DAY, expires=NOW + timedelta(days=60))
    db_session.add(rival)
    await db_session.flush()
    db_session.add_all(
        [
            _session(phone, "rotated", issued=DAY + timedelta(hours=9), expires=GONE,
                     revoked_at=DAY, revoked_reason="rotated"),
            _session(phone, "abandoned", issued=DAY + timedelta(hours=20), expires=GONE),
            _session(phone, "next day", issued=DAY + timedelta(days=1), expires=GONE),
            _session(phone, "in a ring", issued=DAY + timedelta(hours=12), expires=GONE,
                     replaced_by_session_id=rival.id),
            _session(phone, "lately expired", issued=DAY, expires=KEPT),
            _session(phone, "live", issued=NOW, expires=NOW + timedelta(days=90)),
            DeviceActiveDay(device_id=phone.id, day=DAY.date(),
                            first_at=DAY + timedelta(hours=10), last_at=DAY + timedelta(hours=11)),
        ]
    )
    await db_session.commit()

    deleted = await prune_expired_sessions(build_session_factory(prepared_engine), SETTINGS)

    assert deleted == 3
    left = (await db_session.execute(select(AuthSession.refresh_token_hash))).scalars()
    assert sorted(left) == ["in a ring", "lately expired", "live", "rival"]
    days = (
        await db_session.execute(
            select(DeviceActiveDay.day, DeviceActiveDay.first_at, DeviceActiveDay.last_at)
            .order_by(DeviceActiveDay.day)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert [tuple(day) for day in days] == [
        (DAY.date(), DAY + timedelta(hours=9), DAY + timedelta(hours=20)),
        ((DAY + timedelta(days=1)).date(), DAY + timedelta(days=1), DAY + timedelta(days=1)),
    ]


async def test_a_pruned_refresh_token_is_refused(client) -> None:
    login = await do_login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(
            update(AuthSession).values(
                issued_at=GONE - timedelta(days=90), expires_at=GONE
            )
        )
        await session.commit()

    assert await prune_expired_sessions(factory, SETTINGS) == 1

    response = await _refresh(client, login["refresh_token"])
    assert response.status_code == 401


async def test_deleting_a_session_finds_the_rows_it_replaced_by_index(
    db_session,
) -> None:
    """The statement replaced_by_session_id's ON DELETE SET NULL runs for each
    deleted session; without the index, it reads the whole table each time."""
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = (
        await db_session.execute(
            text(
                "EXPLAIN UPDATE ONLY auth_sessions SET replaced_by_session_id = NULL"
                " WHERE :id = replaced_by_session_id"
            ),
            {"id": 1},
        )
    ).scalars()
    assert "idx_auth_sessions_replaced_by" in "\n".join(plan)
