"""Map a push_jobs row's JSONB payload onto transport requests.

The payload is self-contained (title/body + routing hint keys written by
the source generator), so the pipeline never re-joins domain tables.
Extra payload keys ride along stringified — APNs as top-level message
keys, FCM inside `data` (FCM requires string values).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.push.payload import ApnsRequest, FcmRequest, PushKind, _normalize_snapshot_for_apns

_RESERVED = {"title", "body"}
_DEFAULT_TTL_SECONDS = 24 * 3600

# channel → Android notification channel id (client must have registered).
_ANDROID_CHANNELS = {
    "assignment": "assignments",
    "course": "courses",
    "bulletin": "bulletins_sound",
    "system": "system",
    "custom": "bulletins_sound",
}


def _extras(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: value if isinstance(value, str) else str(value)
        for key, value in payload.items()
        if key not in _RESERVED and value is not None
    }


def build_apns_for_job(
    *,
    payload: dict[str, Any],
    channel: str,
    token_value: str,
    bundle_id: str,
    now: datetime | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> ApnsRequest:
    timestamp = int((now or datetime.now(UTC)).timestamp())

    if channel == "schedule":
        # Live Activity content-state update
        kind = payload.get("kind", "schedule")
        if kind == "live_activity_end":
            event = "end"
        else:
            event = "update"
        scenario = payload.get("scenario", "")
        snapshot = {k: v for k, v in payload.items() if k not in ("kind", "scenario", "source_id")}
        normalized_snapshot = _normalize_snapshot_for_apns(snapshot)
        message: dict[str, Any] = {
            "aps": {
                "timestamp": timestamp,
                "event": event,
                "content-state": {
                    "scenario": scenario,
                    **normalized_snapshot,
                },
            },
        }
        if event == "end":
            message["aps"]["dismissal-date"] = timestamp
        return ApnsRequest(
            device_token=token_value,
            topic=f"{bundle_id}.push-type.liveactivity",
            expiration=timestamp + ttl_seconds,
            priority=10,
            message=message,
            kind=PushKind.live_activity,
        )

    if payload.get("kind") == "sync_trigger":
        message = {
            "aps": {"content-available": 1},
            "kind": "sync_trigger",
        }
        return ApnsRequest(
            device_token=token_value,
            topic=bundle_id,
            expiration=timestamp + 300,
            priority=5,
            message=message,
            kind=PushKind.alert,
        )

    # Standard alert (existing logic)
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    message = {
        "aps": {
            "alert": {"title": title, "body": body},
            "badge": 1,
            "sound": "default",
            "mutable-content": 1,
            "thread-id": channel,
        },
        **_extras(payload),
    }
    return ApnsRequest(
        device_token=token_value,
        topic=bundle_id,
        expiration=timestamp + ttl_seconds,
        priority=10,
        message=message,
        kind=PushKind.alert,
    )


def build_fcm_for_job(
    *,
    payload: dict[str, Any],
    channel: str,
    token_value: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> FcmRequest:
    if payload.get("kind") == "sync_trigger":
        return FcmRequest(
            token=token_value,
            title="",
            body="",
            data={"kind": "sync_trigger"},
            ttl_seconds=300,
        )

    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    data = {
        "title": title,
        "body": body,
        "android_channel_id": _ANDROID_CHANNELS.get(channel, "system"),
        **_extras(payload),
    }
    return FcmRequest(
        token=token_value,
        title=title,
        body=body,
        data=data,
        ttl_seconds=ttl_seconds,
    )
