"""/v3/devices — user-scoped device + push-token registration.

Distinct from the legacy /v2/devices (anonymous `device_registrations`):
these rows belong to a logged-in user and feed the Phase-4 user-centric
push fan-out. Frontends keep calling /v2/devices/register for the
anonymous pipeline during the migration (dual-write happens client-side).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import delete as sa_delete, func, select, update

from server.models import DevicePlatform, DeviceRegistration

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import (
    AuthSession,
    DevicePushToken,
    PushTokenKind,
    PushTokenStatus,
    SessionRevokedReason,
    UserDevice,
)
from server.auth.schemas import (
    check_device_class,
    DeviceClass,
    DeviceItem,
    DeviceListV3Response,
    DevicePreferencesV3Request,
    DevicePreferencesV3Response,
    DeviceRegisterV3Request,
    DeviceRegisterV3Response,
    PushTokenIn,
)
from server.db import SessionDep
# Shared so the anonymous endpoint honours the same `auth_trust_forwarded_for`
# setting as login — two different notions of "the client's IP" behind the
# same proxy would make one of the two limits trivially wrong.
from server.routes.auth import _client_ip
from pydantic import BaseModel, Field, model_validator
from typing import Literal

router = APIRouter(prefix="/devices", tags=["devices-v3"])
logger = structlog.get_logger(__name__)

# Rate limits for the unauthenticated anonymous registration. Two keys,
# because neither works alone here.
#
# Per device_id catches the case that actually matters — one client looping
# on the endpoint. The app calls it once per launch, so ten a minute is
# already far more than a healthy device produces.
#
# Per IP has to be much looser than it would be for a login: this is a
# campus app, and a few hundred students behind one NAT egress is the normal
# case, not an attack. It is a ceiling on a single source flooding the table,
# not a per-user limit.
_ANON_DEVICE_MAX_ATTEMPTS = 10
_ANON_DEVICE_WINDOW_SECONDS = 60
_ANON_IP_MAX_ATTEMPTS = 300
_ANON_IP_WINDOW_SECONDS = 60


class AnonymousDeviceRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)
    platform: Literal["apple", "android"]
    device_class: DeviceClass | None = None

    @model_validator(mode="after")
    def device_class_fits_platform(self) -> "AnonymousDeviceRequest":
        check_device_class(self.platform, self.device_class)
        return self
    push_token: str | None = Field(default=None, max_length=512)
    bundle_id: str = Field(default="", max_length=128)
    # The signed-out half of the push opt-out. `PATCH /devices/{id}/
    # preferences` needs a session and writes `user_devices`, so a device
    # with no account had no way to express the setting at all — and this
    # row is the one `custom_push_targeting` filters on. None means "not
    # reported", not "reset to the default".
    server_push_enabled: bool | None = None


class AnonymousDeviceResponse(BaseModel):
    device_id: str
    registered: bool


@router.post("/anonymous", response_model=AnonymousDeviceResponse)
async def register_anonymous_device(
    payload: AnonymousDeviceRequest, request: Request, session: SessionDep
) -> AnonymousDeviceResponse:
    """Register a device that has no account, so operators can reach it.

    A device only appears in the inventory that `custom_push_targeting`
    queries once it has a `device_registrations` row. Signing in creates a
    `user_devices` row instead, which that targeting never sees, so an app
    that has never been signed into was invisible and unreachable — there was
    no way to tell that an Android, iPhone or iPad was running the app at all.

    Deliberately unauthenticated: there is no account to authenticate
    against. The only secret is `device_id`, a client-generated UUID, which
    is the same trust model the retired /v2 registration used.

    Scope is kept narrow: this endpoint may create a row, refresh its token,
    and carry its owner's push preference. It never rewrites `user_id` and
    never touches `linked_user_id` — the signed-in path owns that, and
    clearing it would double-push every bulletin.

    `server_push_enabled` is honoured in both directions. The app is the
    only thing that knows the setting while signed out, so it has to be able
    to both set the opt-out and walk it back; an opt-out-only field would
    strand a user who changed their mind before ever signing in.
    """
    # Two limiters rather than two keys on one, because the two ceilings are
    # deliberately an order of magnitude apart — see the constants above.
    device_limiter = request.app.state.anon_device_limiter
    ip_limiter = request.app.state.anon_ip_limiter
    device_key = payload.device_id
    ip_key = _client_ip(request)
    if not device_limiter.allow(device_key) or not ip_limiter.allow(ip_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too_many_registration_attempts",
        )
    device_limiter.record(device_key)
    ip_limiter.record(ip_key)

    now = datetime.now(UTC)
    existing = (
        await session.execute(
            select(DeviceRegistration).where(
                DeviceRegistration.device_id == payload.device_id
            )
        )
    ).scalar_one_or_none()

    # Apple alert pushes consume `device_token_hex`; Android goes via FCM,
    # whose token lives in `pts_token_hex`. Mirrors the gate in
    # `custom_push_targeting` and `bulletins/matcher` — keep the three in sync.
    is_android = payload.platform == DevicePlatform.android.value
    token = payload.push_token or ""

    if existing is None:
        session.add(
            DeviceRegistration(
                device_id=payload.device_id,
                user_id=f"anon-{payload.device_id}",
                platform=payload.platform,
                device_class=payload.device_class or "",
                pts_token_hex=token if is_android else "",
                device_token_hex=None if is_android else (payload.push_token or None),
                bundle_id=payload.bundle_id,
                attrs_type="",
                apns_env="",
                # Absent means the app had nothing to report, which for a
                # brand-new row is the column default (opted in).
                server_push_enabled=(
                    True
                    if payload.server_push_enabled is None
                    else payload.server_push_enabled
                ),
                created_at=now,
            )
        )
    else:
        existing.platform = payload.platform
        if payload.device_class:
            existing.device_class = payload.device_class
        if payload.server_push_enabled is not None:
            existing.server_push_enabled = payload.server_push_enabled
        # An absent token means "nothing new to report", not "drop the one you
        # have" — the app calls this on every launch, and FCM only hands over a
        # token once it is ready.
        if payload.push_token:
            if is_android:
                existing.pts_token_hex = payload.push_token
            else:
                existing.device_token_hex = payload.push_token
        existing.updated_at = now

    await session.commit()
    logger.info(
        "device.anonymous_registered",
        device_id=payload.device_id,
        platform=payload.platform,
        device_class=payload.device_class,
        created=existing is None,
        has_token=bool(payload.push_token),
        server_push_enabled=payload.server_push_enabled,
    )
    return AnonymousDeviceResponse(device_id=payload.device_id, registered=True)


@router.post("/register", response_model=DeviceRegisterV3Response)
async def register_device(
    payload: DeviceRegisterV3Request, auth: CurrentAuthDep, session: SessionDep
) -> DeviceRegisterV3Response:
    """Upsert the calling user's device (and optionally one push token).
    Idempotent — called on every app launch."""
    now = datetime.now(UTC)
    device = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.user_id == auth.user_id,
                UserDevice.client_device_id == payload.client_device_id,
            )
        )
    ).scalar_one_or_none()
    is_new = device is None
    if is_new:
        device = UserDevice(
            user_id=auth.user_id,
            client_device_id=payload.client_device_id,
            platform=payload.platform,
        )
        session.add(device)
    device.platform = payload.platform
    # Absent means an older client that predates the field, so keep whatever
    # a previous register stored rather than blanking the row back to
    # "unknown form factor" and dropping it out of class-scoped sends.
    if payload.device_class is not None:
        device.device_class = payload.device_class
    if payload.app_version is not None:
        device.app_version = payload.app_version
    if payload.os_version is not None:
        device.os_version = payload.os_version
    if payload.device_model is not None:
        device.device_model = payload.device_model
    # Reported unconditionally on every register call, same as app_version /
    # os_version above — never gated behind a preference toggle. See
    # UserDevice.locale.
    if payload.locale is not None:
        device.locale = payload.locale
    if payload.cloud_sync_enabled is not None:
        if not is_new and device.cloud_sync_enabled != payload.cloud_sync_enabled:
            logger.info(
                "device.cloud_sync_reconciled",
                device_id=str(device.id),
                old=device.cloud_sync_enabled,
                new=payload.cloud_sync_enabled,
            )
        device.cloud_sync_enabled = payload.cloud_sync_enabled
    if payload.bulletin_push_enabled is not None:
        device.bulletin_push_enabled = payload.bulletin_push_enabled
    if payload.server_push_enabled is not None:
        device.server_push_enabled = payload.server_push_enabled
    device.deleted_at = None
    device.last_seen_at = now
    await session.flush()

    push_token_id: int | None = None
    if payload.push_token is not None:
        push_token_id = await _upsert_push_token(
            session, device=device, token=payload.push_token, now=now
        )

    # Phase 4c: if this physical device also has an anonymous v2
    # registration (same client device id), mark it as linked so the
    # anonymous bulletin fan-out stops double-pushing to it.
    await session.execute(
        update(DeviceRegistration)
        .where(DeviceRegistration.device_id == payload.client_device_id)
        .values(linked_user_id=auth.user_id)
    )

    logger.info(
        "device.v3.registered",
        user_id=str(auth.user_id),
        device_id=str(device.id),
        platform=device.platform,
        has_push_token=payload.push_token is not None,
    )
    return DeviceRegisterV3Response(
        device_id=str(device.id), push_token_id=push_token_id
    )


async def _upsert_push_token(
    session, *, device: UserDevice, token: PushTokenIn, now: datetime
) -> int:
    token_hash = hashlib.sha256(token.token_value.encode()).hexdigest()
    existing = (
        await session.execute(
            select(DevicePushToken).where(
                DevicePushToken.provider == token.provider,
                DevicePushToken.token_kind == token.token_kind,
                DevicePushToken.token_hash == token_hash,
                DevicePushToken.scope_key == token.scope_key,
                DevicePushToken.status == PushTokenStatus.active.value,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = DevicePushToken(
            device_id=device.id,
            provider=token.provider,
            token_kind=token.token_kind,
            token_hash=token_hash,
            token_value=token.token_value,
            bundle_id=token.bundle_id,
            topic=token.topic,
            environment=token.environment,
            scope_key=token.scope_key,
        )
        session.add(row)
        await session.flush()
        await _retire_superseded_tokens(session, device=device, token=row, now=now)
        return row.id

    # Same physical token re-registered (possibly from a new device row
    # after reinstall) — move it rather than violating ux_push_token_active.
    existing.device_id = device.id
    existing.bundle_id = token.bundle_id
    existing.topic = token.topic
    existing.environment = token.environment
    await session.flush()
    await _retire_superseded_tokens(session, device=device, token=existing, now=now)
    return existing.id


async def _retire_superseded_tokens(
    session, *, device: UserDevice, token: DevicePushToken, now: datetime
) -> None:
    """Invalidate the device's other active tokens of the same kind and scope.

    A device holds one APNs token, one push-to-start token, one FCM token:
    when the OS rotates one, the value it replaced is dead, and APNs only
    says so once something is pushed to it. Left active, every rotation
    added a row, and a pipeline that selects every active token of a kind
    delivered each push once per row — for a push-to-start, that is one
    Live Activity per stale token.

    `scope_key` only narrows this for Live Activity update tokens, which
    are one per running activity. A standard or push-to-start token is one
    per device whatever its scope says: the push-to-start scope (the
    attributes type name) was introduced after rows with an empty scope
    already existed, and a rotation that also filled the scope in must
    retire the old row rather than sit beside it.
    """
    scope = [
        DevicePushToken.device_id == device.id,
        DevicePushToken.provider == token.provider,
        DevicePushToken.token_kind == token.token_kind,
        DevicePushToken.status == PushTokenStatus.active.value,
        DevicePushToken.id != token.id,
    ]
    if token.token_kind == PushTokenKind.live_activity_update.value:
        scope.append(DevicePushToken.scope_key == token.scope_key)
    await session.execute(
        update(DevicePushToken)
        .where(*scope)
        .values(status=PushTokenStatus.invalidated.value, updated_at=now)
    )


@router.get("", response_model=DeviceListV3Response)
async def list_devices(
    auth: CurrentAuthDep, session: SessionDep
) -> DeviceListV3Response:
    rows = (
        (
            await session.execute(
                select(UserDevice)
                .where(
                    UserDevice.user_id == auth.user_id,
                    UserDevice.deleted_at.is_(None),
                )
                .order_by(UserDevice.created_at)
            )
        )
        .scalars()
        .all()
    )
    return DeviceListV3Response(
        items=[
            DeviceItem(
                id=str(d.id),
                client_device_id=d.client_device_id,
                platform=d.platform,
                app_version=d.app_version,
                os_version=d.os_version,
                last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
                created_at=d.created_at.isoformat(),
            )
            for d in rows
        ]
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str, auth: CurrentAuthDep, session: SessionDep
) -> None:
    """Soft-delete a device and cut its access: revoke its auth sessions
    and invalidate its push tokens — a removed device must not keep
    working tokens.

    `device_id` is the client-owned `client_device_id` (the app's persistent
    UUID) — the identifier the clients hold — scoped to the authed user, not
    the server-side row PK.
    """
    device = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.client_device_id == device_id,
                UserDevice.user_id == auth.user_id,
                UserDevice.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    now = datetime.now(UTC)
    device.deleted_at = now

    device_sessions = (
        await session.execute(
            select(AuthSession).where(
                AuthSession.device_id == device.id,
                AuthSession.revoked_at.is_(None),
            )
        )
    ).scalars()
    for row in device_sessions:
        row.revoked_at = now
        row.revoked_reason = SessionRevokedReason.admin_revoked.value

    tokens = (
        await session.execute(
            select(DevicePushToken).where(
                DevicePushToken.device_id == device.id,
                DevicePushToken.status == PushTokenStatus.active.value,
            )
        )
    ).scalars()
    for token in tokens:
        token.status = PushTokenStatus.invalidated.value

    # Unlink the matching anonymous registration (if any) so the device
    # falls back to the anonymous bulletin pipeline after sign-out.
    await session.execute(
        update(DeviceRegistration)
        .where(
            DeviceRegistration.device_id == device.client_device_id,
            DeviceRegistration.linked_user_id == auth.user_id,
        )
        .values(linked_user_id=None)
    )

    logger.info(
        "device.v3.deleted",
        user_id=str(auth.user_id),
        device_id=str(device.id),
    )


@router.patch("/{device_id}/preferences", response_model=DevicePreferencesV3Response)
async def update_device_preferences(
    device_id: str,
    payload: DevicePreferencesV3Request,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    """Update device preferences (e.g., server_push_enabled).

    `device_id` is the client-owned `client_device_id` (the app's persistent
    UUID), scoped to the authed user — not the server-side row PK.
    """
    device = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.client_device_id == device_id,
                UserDevice.user_id == auth.user_id,
                UserDevice.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device_not_found")
    if payload.server_push_enabled is not None:
        device.server_push_enabled = payload.server_push_enabled
    if payload.bulletin_push_enabled is not None:
        device.bulletin_push_enabled = payload.bulletin_push_enabled
    # Lets a device push a system-language change between register calls.
    # See UserDevice.locale.
    if payload.locale is not None:
        device.locale = payload.locale
    if payload.sync_courses is not None:
        device.sync_courses = payload.sync_courses
    if payload.sync_course_colors is not None:
        device.sync_course_colors = payload.sync_course_colors
    if payload.sync_course_names is not None:
        device.sync_course_names = payload.sync_course_names
    if payload.sync_assignments is not None:
        device.sync_assignments = payload.sync_assignments
    if payload.cloud_sync_enabled is not None:
        device.cloud_sync_enabled = payload.cloud_sync_enabled
    if payload.sync_assignment_reminders is not None:
        device.sync_assignment_reminders = payload.sync_assignment_reminders
    if payload.sync_live_activity is not None:
        device.sync_live_activity = payload.sync_live_activity

    await session.flush()
    await _cleanup_orphaned_sync_data(session, auth.user_id)

    return DevicePreferencesV3Response(
        device_id=device.client_device_id,
        server_push_enabled=device.server_push_enabled,
        bulletin_push_enabled=device.bulletin_push_enabled,
        sync_courses=device.sync_courses,
        sync_course_colors=device.sync_course_colors,
        sync_course_names=device.sync_course_names,
        sync_assignments=device.sync_assignments,
        cloud_sync_enabled=device.cloud_sync_enabled,
        sync_assignment_reminders=device.sync_assignment_reminders,
        sync_live_activity=device.sync_live_activity,
    )


async def _cleanup_orphaned_sync_data(
    session: "AsyncSession", user_id
) -> None:
    """Delete user sync data when no active device needs it.

    Called after a device preference update (post-flush). Checks ALL
    active cloud-enabled devices for the user — the current device's
    updated values are already flushed to the DB at this point.

    Each deleted course/assignment gets a per-entity `delete` changelog
    entry (same shape as DELETE /sync/courses) so clients that later poll
    /changes drop their stale local copies. Overrides ride along via the
    ON DELETE CASCADE on their parent rows and the client-side cascade,
    and tombstones are server-side bookkeeping with no changelog type.
    """
    from server.sync.changelog import append_change, lock_sync_state
    from server.sync.models import (
        UserAssignment,
        UserAssignmentOverride,
        UserCourse,
        UserCourseOverride,
        UserCourseTombstone,
    )

    active_devices = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.user_id == user_id,
                UserDevice.deleted_at.is_(None),
                UserDevice.cloud_sync_enabled.is_(True),
            )
        )
    ).scalars().all()

    any_courses = any(d.sync_courses for d in active_devices)
    any_assignments = any(d.sync_assignments for d in active_devices)
    # (entity_type, entity_id, payload) for the per-row delete entries
    deleted_entries: list[tuple[str, str, dict | None]] = []

    if not any_courses:
        await session.execute(
            sa_delete(UserCourseOverride).where(
                UserCourseOverride.user_id == user_id
            )
        )
        await session.execute(
            sa_delete(UserCourseTombstone).where(
                UserCourseTombstone.user_id == user_id
            )
        )
        rows = (
            await session.execute(
                sa_delete(UserCourse)
                .where(UserCourse.user_id == user_id)
                .returning(UserCourse.id, UserCourse.course_key)
            )
        ).all()
        deleted_entries.extend(
            ("course", str(row.id), {"course_key": row.course_key})
            for row in rows
        )

    if not any_assignments:
        await session.execute(
            sa_delete(UserAssignmentOverride).where(
                UserAssignmentOverride.user_id == user_id
            )
        )
        rows = (
            await session.execute(
                sa_delete(UserAssignment)
                .where(UserAssignment.user_id == user_id)
                .returning(UserAssignment.id)
            )
        ).all()
        deleted_entries.extend(
            ("assignment", str(row.id), None) for row in rows
        )

    if deleted_entries:
        state = await lock_sync_state(session, user_id)
        for entity_type, entity_id, payload in deleted_entries:
            await append_change(
                session,
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                operation="delete",
                payload=payload,
                locked_state=state,
            )
