"""Deregister the devices that are gone, once a day.

A device with no live session cannot call the API, and one nothing has heard
from or reached for `_SILENT_FOR` is most likely deleted or reset. It is
deregistered the way the portal's deregister does it
(`portal/app/routes/devices.py`): soft-deleted, its active push tokens
invalidated and an audit row written. Signing in on it brings it back.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.admin_audit import AdminAuditLog
from server.auth.models import (
    AuthSession,
    DevicePushToken,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.auth.service import lock_accounts
from server.db import in_array, lift_timeouts, session_scope
from server.models import DeviceRegistration

logger = structlog.get_logger(__name__)

_SILENT_FOR = timedelta(days=7)
# The actor of the audit rows this job writes; an operator's is an email.
SYSTEM_ACTOR = "system"


async def retire_dead_devices(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> int:
    """Deregister every device with no live session that was not seen, did
    not announce itself and took no standard push in the last `_SILENT_FOR`.

    An app without a session still launches and takes reminders, so its
    announce (`POST /v3/devices/anonymous`) and an accepted push count as
    signs of life. The devices' accounts are locked first, as a sign-in
    locks them (`lock_accounts`), and the devices are picked again under the
    lock, so a sign-in either finds its device deregistered or keeps it.
    Returns how many it deregistered.
    """
    now = now or datetime.now(UTC)
    since = now - _SILENT_FOR
    live_session = exists().where(
        AuthSession.device_id == UserDevice.id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
    )
    announced = exists().where(
        DeviceRegistration.device_id == UserDevice.client_device_id,
        DeviceRegistration.updated_at > since,
    )
    reached = exists().where(
        DevicePushToken.device_id == UserDevice.id,
        DevicePushToken.token_kind == PushTokenKind.standard.value,
        DevicePushToken.status == PushTokenStatus.active.value,
        DevicePushToken.last_success_at > since,
    )
    dead = (
        UserDevice.deleted_at.is_(None),
        or_(UserDevice.last_seen_at.is_(None), UserDevice.last_seen_at < since),
        ~live_session,
        ~announced,
        ~reached,
    )
    async with session_scope(session_factory) as session:
        await lift_timeouts(session)
        users = (
            await session.execute(select(UserDevice.user_id).where(*dead).distinct())
        ).scalars().all()
        if not users:
            return 0
        await lock_accounts(session, *users)
        retired = (
            await session.execute(
                update(UserDevice)
                .where(in_array(UserDevice.user_id, users), *dead)
                # Not a change the device made: the portal lists devices by
                # updated_at, and its own deregister leaves it alone.
                .values(deleted_at=now, updated_at=UserDevice.updated_at)
                .returning(UserDevice.id, UserDevice.user_id)
            )
        ).all()
        if not retired:
            return 0
        invalidated = (
            await session.execute(
                update(DevicePushToken)
                .where(
                    in_array(DevicePushToken.device_id, [row.id for row in retired]),
                    DevicePushToken.status == PushTokenStatus.active.value,
                )
                .values(status=PushTokenStatus.invalidated.value)
                .returning(DevicePushToken.device_id, DevicePushToken.id)
            )
        ).all()
        token_ids = defaultdict(list)
        for device_id, token_id in invalidated:
            token_ids[device_id].append(token_id)
        session.add_all(
            AdminAuditLog(
                actor=SYSTEM_ACTOR,
                action="device.deregister",
                target_user_id=row.user_id,
                target_device_id=row.id,
                detail={"invalidated_token_ids": sorted(token_ids[row.id])},
            )
            for row in retired
        )
    logger.info(
        "devices.retirement.done", retired=len(retired), tokens=len(invalidated)
    )
    return len(retired)
