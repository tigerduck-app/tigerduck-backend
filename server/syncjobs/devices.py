"""Whose Moodle token the server may use: a user with a device the sync
worker serves."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import InstrumentedAttribute

from server.auth.models import (
    DevicePushToken,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.config import Settings


def has_syncing_device(
    settings: Settings, now: datetime, user_id: InstrumentedAttribute[uuid.UUID]
) -> sa.Exists:
    """Whether the user in `user_id` has a device with TigerSync on that is
    still in use: seen lately, or reached by a push lately. Signing out
    deletes the device. Until one is back, the user's sync jobs wait
    unclaimed (tigerduck-backend#85) and no course announcement poll uses
    their token."""
    return sa.exists().where(
        UserDevice.user_id == user_id,
        UserDevice.deleted_at.is_(None),
        UserDevice.cloud_sync_enabled.is_(True),
        sa.or_(
            UserDevice.last_seen_at
            > now - timedelta(days=settings.sync_job_device_active_days),
            sa.exists().where(
                DevicePushToken.device_id == UserDevice.id,
                DevicePushToken.token_kind == PushTokenKind.standard.value,
                DevicePushToken.status == PushTokenStatus.active.value,
                DevicePushToken.last_success_at
                > now - timedelta(days=settings.sync_job_device_push_days),
            ),
        ),
    )
