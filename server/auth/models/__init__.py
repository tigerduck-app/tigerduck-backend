"""Auth-domain ORM models.

Split by subject rather than kept in one 604-line module. Every name the
old `server.auth.models` exposed is re-exported here, so existing imports
resolve unchanged, and importing this package still registers every table
on `Base.metadata` for Alembic autogenerate.
"""
from .enums import (
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
    AuthSession,
    DevicePushToken,
    UserDevice,
)
from .push import (
    PushDelivery,
    PushJob,
)

__all__ = [
    "AuthSession",
    "CredentialStatus",
    "DevicePushToken",
    "ExternalAccount",
    "ExternalAccountCredential",
    "PushDelivery",
    "PushDeliveryStatus",
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
