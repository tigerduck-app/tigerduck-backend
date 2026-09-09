"""Alert and custom-push payload builders, plus the transport dataclasses
every push path shares.

The Live Activity builders that used to live here went with the v2 sunset.
Live Activity payloads are now built in `job_payloads.build_apns_for_job`,
off a `PushJob`. What stays here is what the bulletin and custom-push paths
still use, plus the snapshot date normalisation both generations need.

References:
  - https://developer.apple.com/documentation/activitykit/starting-and-updating-live-activities-with-activitykit-push-notifications
  - https://developer.apple.com/documentation/usernotifications/sending-push-notifications-using-command-line-tools
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


# Swift's JSONDecoder uses `.deferredToDate` by default, which encodes/
# decodes `Date` as `timeIntervalSinceReferenceDate` — seconds since
# 2001-01-01T00:00:00Z. iOS's ActivityKit uses this decoder when turning
# a payload into `ContentState`, so any Date field we send as an
# ISO8601 string silently fails to decode and iOS drops the push.
#
# Reference epoch in Unix seconds:
_SWIFT_REFERENCE_EPOCH = 978307200  # 2001-01-01T00:00:00Z

# Snapshot fields typed as `Date?` on the Swift side. Must be encoded as
# Double (seconds since reference date) or null.
_DATE_FIELDS = ("countdownTarget", "progressStart")


def _to_swift_reference_seconds(value: Any) -> Any:
    """Convert ISO8601 strings or Unix-second numbers into Swift's
    `timeIntervalSinceReferenceDate` Double. Pass through None/invalid
    shapes untouched so we never crash the dispatcher on bad data."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Assume already in Swift reference seconds. If you later decide
        # to pass Unix seconds instead, convert with value - _SWIFT_REFERENCE_EPOCH.
        return float(value)
    if isinstance(value, str):
        # Accept both "...Z" and "...+00:00" forms.
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() - _SWIFT_REFERENCE_EPOCH
    return value


def _to_unix_seconds(value: Any) -> int | None:
    """Unix seconds for a snapshot date, or None when there is no date.

    The `aps` dictionary's own date keys (`stale-date`, `dismissal-date`)
    are Unix seconds, unlike the content-state's Swift reference seconds.
    """
    reference = _to_swift_reference_seconds(value)
    if isinstance(reference, (int, float)):
        return int(reference + _SWIFT_REFERENCE_EPOCH)
    return None


def _normalize_snapshot_for_apns(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the snapshot with Date fields converted to the
    format Swift's default JSONDecoder expects. Non-destructive."""
    out = dict(snapshot)
    for field in _DATE_FIELDS:
        if field in out:
            out[field] = _to_swift_reference_seconds(out[field])
    return out


class PushKind(StrEnum):
    """Which APNs channel this request targets.

    * `live_activity` — Live Activity update / end. Topic has the
      `.push-type.liveactivity` suffix and APNs refuses delivery otherwise.
    * `alert` — standard user-facing alert/banner push. Topic is the plain
      bundle id.
    """

    live_activity = "live_activity"
    alert = "alert"
    background = "background"


@dataclass(frozen=True)
class ApnsRequest:
    """Transport-level view of a single APNs call."""

    device_token: str
    topic: str
    expiration: int  # unix seconds
    priority: int
    message: dict[str, Any]
    # Default stays on live_activity so the Live Activity path keeps working
    # without changes. Alert callers set `kind=PushKind.alert` explicitly.
    kind: PushKind = field(default=PushKind.live_activity)
    collapse_id: str | None = field(default=None)


def build_alert_request(
    *,
    device_token: str,
    bundle_id: str,
    title: str,
    body: str,
    bulletin_id: int,
    source_url: str,
    canonical_org: str,
    thread_id: str = "bulletin",
    ttl_seconds: int = 7 * 24 * 3600,
    now: datetime | None = None,
    kind: str = "bulletin",
    force_ring: bool = True,
) -> ApnsRequest:
    """Build a standard alert-push APNs request for a bulletin notification.

    `apns-topic` is the plain bundle id (no `.push-type.liveactivity`
    suffix), otherwise iOS drops the push silently. `thread-id` groups
    bulletins under one notification stack on the lock screen.

    Extra keys at the top level (`bulletin_id`, `source_url`,
    `canonical_org`, `kind`, `force_ring`) ride along for the client's
    notification content extension and deep-link handler. `kind`
    distinguishes scraped-bulletin pushes from server-originated custom
    pushes; `force_ring` is stringified ("true"/"false") so it survives
    APNs data-only round-trips on the iOS side. When `force_ring` is
    False the `aps.sound` key is omitted, so iOS delivers the push
    silently (banner-only, no audible alert).
    """
    timestamp = int((now or datetime.now(timezone.utc)).timestamp())
    expiration = timestamp + ttl_seconds
    aps: dict[str, Any] = {
        "alert": {"title": title, "body": body},
        "badge": 1,
        "mutable-content": 1,
        "thread-id": thread_id,
    }
    if force_ring:
        aps["sound"] = "default"
    message: dict[str, Any] = {
        "aps": aps,
        "bulletin_id": bulletin_id,
        "source_url": source_url,
        "canonical_org": canonical_org,
        "kind": kind,
        "force_ring": "true" if force_ring else "false",
    }
    return ApnsRequest(
        device_token=device_token,
        topic=bundle_id,
        expiration=expiration,
        priority=10,
        message=message,
        kind=PushKind.alert,
    )


@dataclass(frozen=True)
class FcmRequest:
    """Transport-level view of a single FCM call. Mirrors `ApnsRequest` so
    the dispatcher can branch on platform without leaking SDK types."""

    token: str
    title: str
    body: str
    data: dict[str, str]
    ttl_seconds: int = 7 * 24 * 3600
    collapse_key: str | None = None


def build_fcm_alert_request(
    *,
    fcm_token: str,
    title: str,
    body: str,
    bulletin_id: int,
    source_url: str,
    canonical_org: str,
    ttl_seconds: int = 7 * 24 * 3600,
    kind: str = "bulletin",
    force_ring: bool = True,
) -> FcmRequest:
    """Build an FCM alert request for a bulletin notification.

    FCM `data` values must all be strings; the Android client parses
    `bulletin_id` back to int. Keeps shape parity with `build_alert_request`
    for APNs. `android_channel_id` routes the notification to either the
    audible (`bulletins_sound`) or silent (`bulletins_silent`) channel —
    the Android client must have both channels registered before any
    custom-push lands.

    `title` and `body` ride inside `data` because the message is sent
    data-only (see `fcm_client.send()` for why) — the Android client reads
    them out of `data` to construct the system notification with the
    deep-link PendingIntent attached.
    """
    return FcmRequest(
        token=fcm_token,
        title=title,
        body=body,
        data={
            "title": title,
            "body": body,
            "bulletin_id": str(bulletin_id),
            "source_url": source_url,
            "canonical_org": canonical_org,
            "kind": kind,
            "force_ring": "true" if force_ring else "false",
            "android_channel_id": "bulletins_sound" if force_ring else "bulletins_silent",
        },
        ttl_seconds=ttl_seconds,
    )


def build_custom_push_popup_apns(
    *,
    device_token: str,
    bundle_id: str,
    title: str,
    body: str,
    notification_id: str,
    force_ring: bool,
    ttl_seconds: int = 24 * 3600,
    now: datetime | None = None,
) -> ApnsRequest:
    """APNs payload for a pure-notification custom push.

    Unlike bulletin pushes, popup pushes do not reference a stored
    bulletin row — the full title/body ride along as top-level keys so
    the client popup renders even if the app was killed between push
    delivery and tap. `notification_id` lets the client de-dupe and
    correlate user interactions back to the originating dispatch.
    """
    timestamp = int((now or datetime.now(timezone.utc)).timestamp())
    aps: dict[str, Any] = {
        "alert": {"title": title, "body": body},
        "badge": 1,
        "mutable-content": 1,
        "thread-id": "custom-push-popup",
    }
    if force_ring:
        aps["sound"] = "default"
    message: dict[str, Any] = {
        "aps": aps,
        "kind": "custom_push_popup",
        "title": title,
        "body": body,
        "notification_id": notification_id,
        "force_ring": "true" if force_ring else "false",
    }
    return ApnsRequest(
        device_token=device_token,
        topic=bundle_id,
        expiration=timestamp + ttl_seconds,
        priority=10,
        message=message,
        kind=PushKind.alert,
    )


def build_custom_push_popup_fcm(
    *,
    fcm_token: str,
    title: str,
    body: str,
    notification_id: str,
    force_ring: bool,
    ttl_seconds: int = 24 * 3600,
) -> FcmRequest:
    """FCM payload for a pure-notification custom push.

    Mirror of `build_custom_push_popup_apns` for Android. All values are
    strings (FCM rejects non-string data). `android_channel_id` picks
    between the audible and silent popup channels.
    """
    return FcmRequest(
        token=fcm_token,
        title=title,
        body=body,
        data={
            "kind": "custom_push_popup",
            "title": title,
            "body": body,
            "notification_id": notification_id,
            "force_ring": "true" if force_ring else "false",
            "android_channel_id": "bulletins_sound" if force_ring else "bulletins_silent",
        },
        ttl_seconds=ttl_seconds,
    )


