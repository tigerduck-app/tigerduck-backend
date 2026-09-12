"""Which queued push jobs will reach which device.

The push pipeline picks a job's recipients only when the job fires
(`server/push/pipeline.py::_materialize`). This replays those rules ahead
of time so the device panel can show what the server has planned for each
device. The portal talks raw SQL and does not import the server package,
so the rules are restated here; keep them in step with `_materialize` and
`server/push/client_versions.py`.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Platforms that take server assignment reminders (APPLE_HANDHELD_PLATFORMS).
_APPLE_HANDHELD = ("ios", "ipados")
#: BACKEND_REMINDERS_MIN_VERSION: the first release that relies on the
#: server for assignment reminders instead of scheduling its own.
_REMINDERS_MIN_VERSION = (2, 1, 0)
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)")

_SCHEDULE = "schedule"
_ASSIGNMENT = "assignment"
_BULLETIN = "bulletin"


def _parse_version(raw: str | None) -> tuple[int, ...] | None:
    if not raw:
        return None
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _takes_assignment_reminders(device: dict[str, Any]) -> bool:
    if device["platform"] not in _APPLE_HANDHELD:
        return False
    version = _parse_version(device.get("app_version"))
    # Fails closed like the pipeline: an unplaceable version gets nothing.
    return version is not None and version >= _REMINDERS_MIN_VERSION


def _token_kind(job: dict[str, Any]) -> str:
    if job["channel"] != _SCHEDULE:
        return "standard"
    if job["payload"].get("kind") == "live_activity_end":
        return "live_activity_update"
    return "push_to_start"


def payload_of(raw: Any) -> dict[str, Any]:
    """asyncpg hands JSONB back as text unless a codec is registered."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def recipients(
    job: dict[str, Any],
    devices: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The devices `job` is addressed to, each with whether it holds the
    token the push needs. A device without one is still listed, marked
    not ready: the job is planned for it but will not arrive.

    `job["payload"]` must already be a dict (see `payload_of`); `tokens`
    must already be filtered to active, unexpired rows.
    """
    payload = job["payload"]
    kind = _token_kind(job)
    is_end = kind == "live_activity_update"
    sync_trigger = job["scenario"] == "sync_trigger"
    targets = payload.get("target_device_ids") if job["channel"] == _BULLETIN else None

    out = []
    for device in devices:
        device_id = str(device["id"])
        if device.get("deleted_at") is not None:
            continue
        # macOS is foreground-only and takes no push at all.
        if device["platform"] == "macos":
            continue
        if job.get("device_id") is not None and str(job["device_id"]) != device_id:
            continue
        if sync_trigger and (
            device.get("cloud_sync_enabled") is False
            or payload.get("source_device_id") == device_id
        ):
            continue
        if job["channel"] == _ASSIGNMENT and not (
            device.get("cloud_sync_enabled") is not False
            and device.get("sync_assignment_reminders") is not False
            and _takes_assignment_reminders(device)
        ):
            continue
        if job["channel"] == _BULLETIN:
            if device.get("bulletin_push_enabled") is False:
                continue
            if targets is not None and device_id not in targets:
                continue
        ready = any(
            str(t["device_id"]) == device_id
            and t["token_kind"] == kind
            and (not is_end or t["scope_key"] == payload.get("activity_id"))
            for t in tokens
        )
        out.append({"device_id": device_id, "token_ready": ready})
    return out


def summary(job: dict[str, Any]) -> dict[str, str]:
    """A title and a line of detail to show for the job, from whichever
    payload shape its channel uses."""
    p = job["payload"]
    if job["channel"] == _ASSIGNMENT:
        # Copy is written per recipient at send time; show its inputs.
        return {
            "title": str(p.get("assignment_title") or ""),
            "body": str(p.get("course_name") or ""),
        }
    return {
        "title": str(p.get("title") or ""),
        "body": str(p.get("body") or p.get("subtitle") or ""),
    }
