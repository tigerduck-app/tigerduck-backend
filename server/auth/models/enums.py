"""Status and kind enumerations for the auth and push tables."""

from __future__ import annotations
from enum import StrEnum


class UserStatus(StrEnum):
    active = "active"
    suspended = "suspended"
    pending_deletion = "pending_deletion"
class CredentialStatus(StrEnum):
    active = "active"
    expired = "expired"
    invalid = "invalid"
    revoked = "revoked"
class UserDevicePlatform(StrEnum):
    ios = "ios"
    ipados = "ipados"
    macos = "macos"
    windows = "windows"
    watchos = "watchos"
    wearos = "wearos"
    android = "android"
    web = "web"


#: iPhone and iPad: the only platforms the backend sends assignment
#: reminders to, and the only ones a reauth push is filed for. Listing what
#: is included rather than what is not makes a platform added later fail
#: closed. A tuple, so it serves a Python `in` check and a SQL `IN (...)`
#: alike, in a stable order.
APPLE_HANDHELD_PLATFORMS: tuple[str, ...] = (
    UserDevicePlatform.ios.value,
    UserDevicePlatform.ipados.value,
)


class SessionRevokedReason(StrEnum):
    logout = "logout"
    rotated = "rotated"
    reuse_detected = "reuse_detected"
    expired = "expired"
    admin_revoked = "admin_revoked"
    credential_revoked = "credential_revoked"
class PushTokenProvider(StrEnum):
    apns = "apns"
    fcm = "fcm"
class PushTokenKind(StrEnum):
    standard = "standard"
    push_to_start = "push_to_start"
    live_activity_update = "live_activity_update"
class PushTokenStatus(StrEnum):
    active = "active"
    invalidated = "invalidated"
    expired = "expired"
class PushJobStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    sent = "sent"
    partial_failed = "partial_failed"
    failed = "failed"
    cancelled = "cancelled"
class PushDeliveryStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"
