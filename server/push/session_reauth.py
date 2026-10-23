"""Ask a device left without a TigerSync session to sign in again.

A device whose backend session is gone, and whose app cannot sign in again by
itself, stays out of TigerSync while the app still shows the account as signed
in, and nothing tells the user (tigerduck-backend#125). This scan files a
`reauth_required` push with reason `session` for that device alone, at most
once every 7 days, and only in the daytime at school, since the push rings.
The pipeline asks again before each try to send, and holds a try that falls
at night or right after an announce (`pipeline._materialize`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Select, and_, exists, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import (
    APPLE_HANDHELD_PLATFORMS,
    AuthSession,
    DevicePushToken,
    PushTokenKind,
    PushTokenStatus,
    SessionRevokedReason,
    UserDevice,
)
from server.config import Settings
from server.db import in_array, session_scope
from server.models import DeviceRegistration
from server.push.notification_copy import REAUTH_SCENARIO, SESSION_REAUTH_REASON
from server.push.reminders import file_jobs

logger = structlog.get_logger(__name__)

# macOS takes no `reauth_required` push. Android is left out until its 2.4.0
# ships: 2.3.1 keeps a live session it stops using, so it would be asked weekly.
_PLATFORMS = APPLE_HANDHELD_PLATFORMS
# The apps announce the device on every launch (`POST /v3/devices/anonymous`),
# signed in or not; a device not announced for this long is not in use.
_SEEN_WITHIN = timedelta(days=3)
# Long enough for an app to sign in again by itself after its session ended.
_SIGN_IN_GRACE = timedelta(hours=1)
# An app refreshes its session, or signs in again, right after it announces;
# the scan leaves an announce this recent for a later run, and the pipeline
# holds a try until the announce is this old.
_ANNOUNCE_SETTLE = timedelta(minutes=10)
# An app that holds its session refreshes it once its access token runs out,
# 15 minutes after it was issued (`auth_access_token_ttl_seconds`), so a live
# session issued this long before a launch is not held.
_UNTOUCHED = timedelta(hours=24)
_REPEAT_AFTER = timedelta(days=7)
# After about four pushes a device whose session ended this long ago is left
# alone; one with a live session it does not use is asked on.
_GIVE_UP_AFTER = timedelta(days=28)
# Hours at school, in `course_reminder_timezone`, the push is filed and sent
# in.
_SEND_HOURS = range(9, 21)


def sessionless_devices(now: datetime) -> Select:
    """The devices, with their users and last announce (`announced_at`),
    whose app keeps launching without a session it can use, as of `now`.

    Such a device was announced within `_SEEN_WITHIN`, holds an active
    standard push token, has TigerSync on, and its newest session did not end
    in a sign-out. Then either it has a live session and its newest session
    was issued more than `_UNTOUCHED` before the last announce, or it has no
    live session and was announced more than `_SIGN_IN_GRACE` after its newest
    session was revoked or expired, at most `_GIVE_UP_AFTER` before `now`.
    `server_push_enabled`, which only operator pushes read, is not read here,
    as for the credential reauth push.

    A device's newest session leaves out the rivals a duplicate refresh left
    beside the one it went on with, which superseded them when it was first
    used (`settle_contest` in server/auth/service.py). They can be newer, and
    keep saying `superseded` after it ended in a sign-out.
    """
    newest = (
        select(
            AuthSession.issued_at,
            AuthSession.expires_at,
            AuthSession.revoked_at,
            AuthSession.revoked_reason,
        )
        .where(
            AuthSession.device_id == UserDevice.id,
            AuthSession.revoked_reason.is_distinct_from(
                SessionRevokedReason.superseded.value
            ),
        )
        .order_by(AuthSession.id.desc())
        .limit(1)
        .lateral()
    )
    live = exists().where(
        # The user's live sessions, through idx_auth_sessions_user_active.
        AuthSession.user_id == UserDevice.user_id,
        AuthSession.device_id == UserDevice.id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
    )
    pushable = exists().where(
        DevicePushToken.device_id == UserDevice.id,
        DevicePushToken.token_kind == PushTokenKind.standard.value,
        DevicePushToken.status == PushTokenStatus.active.value,
        or_(DevicePushToken.expires_at.is_(None), DevicePushToken.expires_at > now),
    )
    announced = DeviceRegistration.updated_at
    ended = func.coalesce(newest.c.revoked_at, newest.c.expires_at)
    return (
        select(UserDevice.id, UserDevice.user_id, announced.label("announced_at"))
        .join(
            DeviceRegistration,
            DeviceRegistration.device_id == UserDevice.client_device_id,
        )
        .join(newest, true())
        .where(
            UserDevice.deleted_at.is_(None),
            UserDevice.cloud_sync_enabled.is_(True),
            UserDevice.platform.in_(_PLATFORMS),
            announced > now - _SEEN_WITHIN,
            pushable,
            newest.c.revoked_reason.is_distinct_from(
                SessionRevokedReason.logout.value
            ),
            or_(
                and_(live, announced > newest.c.issued_at + _UNTOUCHED),
                and_(
                    ~live,
                    announced > ended + _SIGN_IN_GRACE,
                    ended >= now - _GIVE_UP_AFTER,
                ),
            ),
        )
    )


async def announced_while_sessionless(
    session: AsyncSession, device_id: uuid.UUID, now: datetime
) -> datetime | None:
    """When `device_id` last announced itself, while `sessionless_devices`
    still finds it at `now`; None once it does not."""
    found = await session.execute(
        sessionless_devices(now).where(UserDevice.id == device_id).limit(1)
    )
    row = found.first()
    return row.announced_at if row is not None else None


def send_after(
    now: datetime, announced_at: datetime, school: ZoneInfo
) -> datetime | None:
    """When a push to a device last announced at `announced_at` may try to go
    out, or None when it may at `now`: once the refresh or sign-in the app
    makes right after an announce has had `_ANNOUNCE_SETTLE` to land, and in
    `_SEND_HOURS` at school."""
    at = max(now, announced_at + _ANNOUNCE_SETTLE)
    local = at.astimezone(school)
    if local.hour not in _SEND_HOURS:
        day = local.date() + timedelta(days=int(local.hour >= _SEND_HOURS.stop))
        at = datetime.combine(day, time(_SEND_HOURS.start), tzinfo=school)
    return at.astimezone(UTC) if at > now else None


async def scan_sessionless_devices(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """File the push for each device `sessionless_devices` finds that was not
    asked in the last `_REPEAT_AFTER`, and return how many were filed.

    A device announced within `_ANNOUNCE_SETTLE` waits for a later run. A
    device is marked and its job filed in one transaction, and the mark is
    only taken over one at least `_REPEAT_AFTER` old, so a run that overlaps
    another, or follows a restart, files nothing twice.
    """
    now = now or datetime.now(UTC)
    school = ZoneInfo(settings.course_reminder_timezone)
    if now.astimezone(school).hour not in _SEND_HOURS:
        return 0
    async with session_scope(session_factory) as session:
        found = (
            await session.execute(
                sessionless_devices(now).where(
                    DeviceRegistration.updated_at <= now - _ANNOUNCE_SETTLE
                )
            )
        ).all()
        if not found:
            return 0
        claimed = (
            await session.execute(
                update(UserDevice)
                .where(
                    in_array(UserDevice.id, [row.id for row in found]),
                    or_(
                        UserDevice.session_reauth_pushed_at.is_(None),
                        UserDevice.session_reauth_pushed_at <= now - _REPEAT_AFTER,
                    ),
                )
                # Not a change the device made: the portal lists devices by
                # updated_at.
                .values(session_reauth_pushed_at=now, updated_at=UserDevice.updated_at)
                .returning(UserDevice.id, UserDevice.user_id)
            )
        ).all()
        created = await file_jobs(
            session,
            [
                {
                    "user_id": user_id,
                    "device_id": device_id,
                    # Apart from the device's earlier ones, which a sent job
                    # still holds in ux_push_jobs_dedupe_active.
                    "dedupe_key": (
                        f"system:device:{device_id}:{REAUTH_SCENARIO}"
                        f":{SESSION_REAUTH_REASON}:{int(now.timestamp())}"
                    ),
                    "channel": "system",
                    "scenario": REAUTH_SCENARIO,
                    "fire_at": now,
                    "payload": {
                        "kind": REAUTH_SCENARIO,
                        "reason": SESSION_REAUTH_REASON,
                    },
                }
                for device_id, user_id in claimed
            ],
        )
    if created:
        logger.info("push.session_reauth.filed", found=len(found), created=created)
    return created
