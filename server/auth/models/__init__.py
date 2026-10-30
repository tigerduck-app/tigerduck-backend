"""Auth-domain ORM models.

Split by subject rather than kept in one 604-line module. Every name the
old `server.auth.models` exposed is re-exported here, so existing imports
resolve unchanged, and importing this package still registers every table
on `Base.metadata` for Alembic autogenerate.
"""
from .enums import (
    APPLE_HANDHELD_PLATFORMS,
    CredentialStatus,
    PushDeliveryStatus,
    PushJobStatus,
    PushTokenKind,
    PushTokenProvider,
    PushTokenStatus,
    SessionRevokedReason,
    UserDevicePlatform,
    UserStatus,
)
from .user import (
    ExternalAccount,
    ExternalAccountCredential,
    User,
)
from .device import (
    PUSH_TOKEN_ACTIVE_WHERE,
    AuthSession,
    DeviceActiveDay,
    DevicePushToken,
    UserDevice,
)
from .push import (
    PUSH_JOB_DEDUPE_ACTIVE_STATUSES,
    PUSH_JOB_DEDUPE_ACTIVE_WHERE,
    PushDelivery,
    PushJob,
)

__all__ = [
    "APPLE_HANDHELD_PLATFORMS",
    "AuthSession",
    "CredentialStatus",
    "DeviceActiveDay",
    "DevicePushToken",
    "ExternalAccount",
    "ExternalAccountCredential",
    "PushDelivery",
    "PushDeliveryStatus",
    "PUSH_JOB_DEDUPE_ACTIVE_STATUSES",
    "PUSH_JOB_DEDUPE_ACTIVE_WHERE",
    "PUSH_TOKEN_ACTIVE_WHERE",
    "PushJob",
    "PushJobStatus",
    "PushTokenKind",
    "PushTokenProvider",
    "PushTokenStatus",
    "SessionRevokedReason",
    "User",
    "UserDevice",
    "UserDevicePlatform",
    "UserStatus",
]
