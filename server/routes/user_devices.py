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
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete as sa_delete, func, select, update

from server.models import DevicePlatform, DeviceRegistration

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import (
    AuthSession,
    DevicePushToken,
    PushTokenStatus,
    SessionRevokedReason,
    UserDevice,
)
from server.auth.schemas import (
    DeviceItem,
    DeviceListV3Response,
    DevicePreferencesV3Request,
    DevicePreferencesV3Response,
    DeviceRegisterV3Request,
    DeviceRegisterV3Response,
    PushTokenIn,
)
from server.db import SessionDep
from pydantic import BaseModel, Field
from typing import Literal

router = APIRouter(prefix="/devices", tags=["devices-v3"])
logger = structlog.get_logger(__name__)


class AnonymousDeviceRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)
    platform: Literal["apple", "android"]
    device_class: Literal["iphone", "ipad", "android"] | None = None
    push_token: str | None = Field(default=None, max_length=512)
    bundle_id: str = Field(default="", max_length=128)


class AnonymousDeviceResponse(BaseModel):
    device_id: str
    registered: bool


@router.post("/anonymous", response_model=AnonymousDeviceResponse)
async def register_anonymous_device(
    payload: AnonymousDeviceRequest, session: SessionDep
) -> AnonymousDeviceResponse:
    """Register a device that has no account, so operators can reach it.

    A device only appears in the inventory that `custom_push_targeting`
    queries once it has a `device_registrations` row. Signing in creates a
    `user_devices` row instead, which that targeting never sees, so an app
    that has never been signed into was invisible and unreachable — there was
    no way to tell that an Android, iPhone or iPad was running the app at all.

    Deliberately unauthenticated: there is no account to authenticate
    against. The only secret is `device_id`, a client-generated UUID, which
    is the same trust model the retired /v2 registration used. To keep the
    blast radius of a guessed id small this endpoint may only create a row or
    refresh its token — it never rewrites `user_id`, never touches
    `linked_user_id` (the signed-in path owns that, and clearing it would
    double-push every bulletin), and never re-enables push for a device whose
    owner opted out.
    """
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
                created_at=now,
            )
        )
    else:
        existing.platform = payload.platform
        if payload.device_class:
            existing.device_class = payload.device_class
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
    if payload.app_version is not None:
        device.app_version = payload.app_version
    if payload.os_version is not None:
        device.os_version = payload.os_version
    if payload.cloud_sync_enabled is not None:
        if not is_new and device.cloud_sync_enabled != payload.cloud_sync_enabled:
            logger.info(
                "device.cloud_sync_reconciled",
                device_id=str(device.id),
                old=device.cloud_sync_enabled,
                new=payload.cloud_sync_enabled,
            )
        device.cloud_sync_enabled = payload.cloud_sync_enabled
    device.deleted_at = None
    device.last_seen_at = now
    await session.flush()

    push_token_id: int | None = None
    if payload.push_token is not None:
        push_token_id = await _upsert_push_token(
            session, device=device, token=payload.push_token, now=now
        )

    # Phase 4c (review 1.8): if this physical device also has an anonymous
    # v2 registration (same client device id), mark it as linked so the
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
        return row.id

    # Same physical token re-registered (possibly from a new device row
    # after reinstall) — move it rather than violating ux_push_token_active.
    existing.device_id = device.id
    existing.bundle_id = token.bundle_id
    existing.topic = token.topic
    existing.environment = token.environment
    await session.flush()
    return existing.id


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
    and invalidate its push tokens (security review — a removed device must
    not keep working tokens).

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

    await session.flush()
    await _cleanup_orphaned_sync_data(session, auth.user_id)

    return DevicePreferencesV3Response(
        device_id=device.client_device_id,
        server_push_enabled=device.server_push_enabled,
        sync_courses=device.sync_courses,
        sync_course_colors=device.sync_course_colors,
        sync_course_names=device.sync_course_names,
        sync_assignments=device.sync_assignments,
        cloud_sync_enabled=device.cloud_sync_enabled,
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
