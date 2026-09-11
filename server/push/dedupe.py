"""Dedupe keys for the Live Activity jobs on the `schedule` channel.

Two routes write these jobs and the pipeline reads them back, so the
shapes live in one place. Both are filed per device: each of the user's
phones and tablets posts its own schedule and runs its own activities, and
`ux_push_jobs_dedupe_active` is per (user, key), so a key without the
device in it made the second device's rows collide with the first's — its
schedule was dropped, and its activity's end job was overwritten with the
first device's id and never reached it. Both routes refuse a session
without a device id, so every key carries one.

Neither key is scoped to an occurrence on purpose: the client's source id
already is ("{course_no}_{yyyyMMdd}_{period}" for a class, the assignment
id for an assignment), so a weekly class posts a fresh key each week and a
re-sync of the same occurrence finds its own sent job and leaves it alone.
"""

from __future__ import annotations

import uuid

# The channel both routes below file their jobs on, and the one
# `push/pipeline.py` keys its start/end split off (`job.channel ==
# "schedule"`). Named `SCHEDULE_CHANNEL` here rather than the bare
# `CHANNEL` its original home used, since this module is a shared home for
# more than one channel's worth of key shapes.
SCHEDULE_CHANNEL = "schedule"


def schedule_prefix(device_id: uuid.UUID) -> str:
    return f"schedule:{device_id}:"


def schedule_key(device_id: uuid.UUID, source_id: str, scenario: str) -> str:
    """A start job: one per (device, occurrence, scenario)."""
    return f"{schedule_prefix(device_id)}{source_id}:{scenario}"


def activity_end_prefix(device_id: uuid.UUID) -> str:
    return f"la_end:{device_id}:"


def activity_end_key(device_id: uuid.UUID, activity_id: str) -> str:
    """An end job: one per (device, running activity)."""
    return f"{activity_end_prefix(device_id)}{activity_id}"


def activity_id(source_id: str, scenario: str) -> str:
    """The id a Live Activity carries in its attributes, composed the same
    way on both sides — see `composedActivityId` on the client's
    LiveActivitySnapshot, which is "{scenario}::{sourceId}". Composing it
    here rather than trusting a value sent by the client keeps the end
    job's dedupe key, the update token's registered scope, and the
    client's own id always in agreement.
    """
    return f"{scenario}::{source_id}"
