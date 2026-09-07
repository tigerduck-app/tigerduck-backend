"""Retention job tests — ended/failed/cancelled Live Activity tokens are
pruned past their retention window, active rows are untouched."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from server.config import Settings
from server.db import build_session_factory
from server.models import (
    DeviceRegistration,
    LiveActivityTokenStatus,
    LiveActivityUpdateToken,
)
from server.scheduler.retention import prune_terminal_activity_tokens

pytestmark = pytest.mark.asyncio(loop_scope="session")


DEVICE_ID = "device-retention"


async def _seed_device(s: AsyncSession) -> None:
    # The v2 register endpoint is retired (410 Gone); insert the
    # device_registrations row the token FK points at directly instead.
    s.add(
        DeviceRegistration(
            device_id=DEVICE_ID,
            user_id="user-retention",
            pts_token_hex="a" * 128,
            bundle_id="org.ntust.app.TigerDuck",
            attrs_type="a",
            apns_env="development",
        )
    )
    await s.commit()


async def _seed_token(
    factory,
    *,
    activity_id: str,
    status: str,
    updated_delta: timedelta,
) -> None:
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(
            LiveActivityUpdateToken(
                activity_id=activity_id,
                device_id=DEVICE_ID,
                source_id=activity_id.split("::")[-1],
                scenario="inClass",
                update_token_hex="c" * 80,
                countdown_target=None,
                snapshot_json={"sourceId": activity_id.split("::")[-1]},
                status=status,
                attempts=1,
            )
        )
        await s.commit()
        # Tests can't rely on created_at/updated_at since SQLAlchemy stamps
        # them with NOW() at insert. Backdate explicitly so the cutoff logic
        # has something to match against.
        await s.execute(
            update(LiveActivityUpdateToken)
            .where(LiveActivityUpdateToken.activity_id == activity_id)
            .values(updated_at=now - updated_delta)
        )
        await s.commit()


async def test_retention_prunes_terminal_rows_past_cutoff(
    db_session: AsyncSession,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    # db_session resets the schema for this test; seed the device through
    # it, then drive the pruner with its own factory like the scheduler
    # runtime does.
    await _seed_device(db_session)
    factory = build_session_factory(prepared_engine)

    retention = timedelta(days=test_settings.live_activity_token_retention_days)
    await _seed_token(
        factory,
        activity_id="inClass::old-ended",
        status=LiveActivityTokenStatus.ended.value,
        updated_delta=retention + timedelta(days=1),
    )
    await _seed_token(
        factory,
        activity_id="inClass::old-cancelled",
        status=LiveActivityTokenStatus.cancelled.value,
        updated_delta=retention + timedelta(days=1),
    )
    await _seed_token(
        factory,
        activity_id="inClass::old-failed",
        status=LiveActivityTokenStatus.failed.value,
        updated_delta=retention + timedelta(days=1),
    )
    await _seed_token(
        factory,
        activity_id="inClass::recent-ended",
        status=LiveActivityTokenStatus.ended.value,
        updated_delta=timedelta(days=1),
    )
    await _seed_token(
        factory,
        activity_id="inClass::old-but-active",
        status=LiveActivityTokenStatus.active.value,
        updated_delta=retention + timedelta(days=1),
    )

    deleted = await prune_terminal_activity_tokens(factory, test_settings)
    assert deleted == 3

    async with factory() as s:
        rows = (
            await s.execute(
                select(LiveActivityUpdateToken.activity_id).order_by(
                    LiveActivityUpdateToken.activity_id
                )
            )
        ).scalars().all()
    assert rows == ["inClass::old-but-active", "inClass::recent-ended"]
