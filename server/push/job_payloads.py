"""Map a push_jobs row's JSONB payload onto transport requests.

The payload is self-contained (title/body + routing hint keys written by
the source generator), so the pipeline never re-joins domain tables.
Extra payload keys ride along stringified — APNs as top-level message
keys, FCM inside `data` (FCM requires string values).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from server.push.dedupe import SCHEDULE_CHANNEL
from server.push.payload import (
    ApnsRequest,
    FcmRequest,
    PushKind,
    _normalize_snapshot_for_apns,
    _to_unix_seconds,
)

_RESERVED = {"title", "body"}
_DEFAULT_TTL_SECONDS = 24 * 3600

# Keys a schedule job's payload carries alongside the client's snapshot:
# written by `/schedule/sync` and `/live-activities/register`, read by the
# pipeline, never part of the content-state.
_ACTIVITY_ROUTING_KEYS = ("kind", "activity_id", "source_id")

# `attributes-type` for a push-to-start when the token row does not say.
# The client registers its push-to-start token under the ActivityAttributes
# type name as `scope_key`, and `_send_one` passes that through; this is
# only for a row registered before it did.
_DEFAULT_ACTIVITY_ATTRIBUTES_TYPE = "TigerDuckActivityAttributes"

# channel → Android notification channel id (client must have registered).
_ANDROID_CHANNELS = {
    "assignment": "assignments",
    "course": "courses",
    "bulletin": "bulletins_sound",
    "system": "system",
    "custom": "bulletins_sound",
}

# FCM allows at most 4 active collapse keys per device at once. The
# schedule channel is keyed per activity in `_collapse_key` instead.
_COLLAPSE_KEYS = {
    "sync_trigger": "sync",
    "course": "reminder",
    "assignment": "reminder",
}


def _collapse_key(channel: str, payload: dict[str, Any]) -> str | None:
    kind = payload.get("kind", "")
    if kind == "sync_trigger":
        return _COLLAPSE_KEYS["sync_trigger"]
    if channel == SCHEDULE_CHANNEL:
        # APNs keeps one undelivered push per collapse id. One id for the
        # whole channel let class B's start evict class A's while the phone
        # was asleep, and A never appeared. Per activity, so that only a
        # later push for the *same* activity replaces an earlier one — an
        # end overtaking an undelivered start is the right outcome, the
        # class is over. Hashed: the id is up to 160 characters and APNs
        # caps the header at 64 bytes.
        activity_id = payload.get("activity_id")
        if not activity_id:
            return None
        return "la:" + hashlib.sha256(activity_id.encode()).hexdigest()[:32]
    return _COLLAPSE_KEYS.get(channel)


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
    attributes_type: str | None = None,
) -> ApnsRequest:
    """Build the APNs request for one delivery of `payload` on `channel`.

    `attributes_type` is only read for a Live Activity start: it is the
    `ActivityAttributes` type name the client registered its push-to-start
    token under (the token's `scope_key`), which ActivityKit needs to know
    which attributes struct to decode `attributes` into.
    """
    timestamp = int((now or datetime.now(UTC)).timestamp())
    collapse = _collapse_key(channel, payload)

    if channel == SCHEDULE_CHANNEL:
        # The job payload is the client's snapshot flattened together with
        # the routing keys the source route added alongside it. Strip only
        # those routing keys — every remaining entry is a LiveActivitySnapshot
        # field and has to survive the trip back. `scenario` in particular:
        # it is non-optional on the client, so hoisting it out of the
        # snapshot makes the state undecodable.
        snapshot = {
            k: v for k, v in payload.items() if k not in _ACTIVITY_ROUTING_KEYS
        }
        normalized_snapshot = _normalize_snapshot_for_apns(snapshot)
        aps: dict[str, Any] = {
            "timestamp": timestamp,
            # `TigerDuckActivityAttributes.ContentState` is a single
            # `snapshot` property, so the state nests under that key. A flat
            # object fails to decode, and ActivityKit discards a push it
            # cannot decode — for an "end" that means the activity is never
            # dismissed and the Dynamic Island sits there until iOS's own
            # stale cleanup; for a "start" it means nothing appears at all.
            "content-state": {"snapshot": normalized_snapshot},
        }
        if payload.get("kind") == "live_activity_end":
            aps["event"] = "end"
            aps["dismissal-date"] = timestamp
        else:
            # A schedule job starts an activity that is not running yet, so
            # it goes to the push-to-start token and has to carry everything
            # `Activity.request` would on the device: the attributes type
            # and value ActivityKit instantiates, and the alert Apple
            # requires on a start so the person is told why something just
            # appeared. `activityId` is the one attribute the client
            # defines, and `/schedule/sync` composes it the same way the
            # client does, so the activity this push starts is the one the
            # client will later register an update token for and end.
            activity_id = payload.get("activity_id")
            if not activity_id:
                # The pipeline settles such a job before it gets here; any
                # other caller is told so rather than handed a KeyError.
                raise ValueError("schedule start job carries no activity_id")
            aps["event"] = "start"
            aps["attributes-type"] = (
                attributes_type or _DEFAULT_ACTIVITY_ATTRIBUTES_TYPE
            )
            aps["attributes"] = {"activityId": activity_id}
            aps["alert"] = {
                "title": str(snapshot.get("title") or ""),
                "body": str(snapshot.get("subtitle") or ""),
            }
            # Same marker the client sets locally (`staleDate` =
            # `countdownTarget`), so a started-by-push activity greys out at
            # the same moment if its end push is lost.
            stale_at = _to_unix_seconds(snapshot.get("countdownTarget"))
            if stale_at is not None:
                aps["stale-date"] = stale_at
        message: dict[str, Any] = {"aps": aps}
        return ApnsRequest(
            device_token=token_value,
            topic=f"{bundle_id}.push-type.liveactivity",
            expiration=timestamp + ttl_seconds,
            priority=10,
            message=message,
            kind=PushKind.live_activity,
            collapse_id=collapse,
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
            kind=PushKind.background,
            collapse_id=collapse,
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
        collapse_id=collapse,
    )


def build_fcm_for_job(
    *,
    payload: dict[str, Any],
    channel: str,
    token_value: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> FcmRequest:
    collapse = _collapse_key(channel, payload)

    if payload.get("kind") == "sync_trigger":
        return FcmRequest(
            token=token_value,
            title="",
            body="",
            data={"kind": "sync_trigger"},
            ttl_seconds=300,
            collapse_key=collapse,
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
        collapse_key=collapse,
    )
