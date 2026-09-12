"""Phase 4a: generate assignment due-date reminder push_jobs.

Scan-based (default every 300s): eligible = not deleted, not submitted,
local_status not in (locally_completed/ignored/archived), due within the
scan window, and the user's `notification` settings document has
assignments enabled (no document → enabled with the server default
offsets). One push_job per (assignment, offset) with the due-date epoch
embedded in the dedupe key — `ux_push_jobs_dedupe_active` also covers
sent states (security fix 1.2), so a due-date change must mint a NEW key;
the scan cancels pending jobs whose key is no longer valid (due changed,
assignment completed/ignored/deleted, or reminders disabled).

A key stays valid even after its fire time has passed (the pipeline is
about to deliver it) — only keys that no longer correspond to an
eligible (assignment, offset, due) tuple are cancelled.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import PushJob, PushJobStatus
from server.config import Settings
from server.db import session_scope
from server.push.notification_copy import has_reminder_copy_inputs
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserSettingsDocument,
)

logger = structlog.get_logger(__name__)

_EXCLUDED_LOCAL_STATUSES = {"locally_completed", "ignored", "archived"}
CHANNEL = "assignment"


def _fmt_offset(hours: float) -> str:
    return f"{hours:g}"


def reminder_key_prefix(moodle_assignment_id: int) -> str:
    """What every reminder dedupe key for this assignment starts with,
    whatever its offset and due date.

    `push/submission_cancel.py` cancels an assignment's pending reminders
    by this prefix once it is submitted, and `_dedupe_key` is built on it,
    so the two cannot drift apart. It ends after `reminder_`, past the
    delimiter that closes the id, so assignment 12's prefix never matches
    a key of assignment 123.
    """
    return f"assignment:moodle:{moodle_assignment_id}:reminder_"


def _dedupe_key(moodle_assignment_id: int, offset: float, due_epoch: int) -> str:
    return (
        f"{reminder_key_prefix(moodle_assignment_id)}"
        f"{_fmt_offset(offset)}h:{due_epoch}"
    )


async def _notification_prefs(
    session: AsyncSession, user_ids: set[uuid.UUID], settings: Settings
) -> dict[uuid.UUID, tuple[bool, list[float]]]:
    """user_id → (enabled, offsets). Missing document → server defaults."""
    docs = (
        (
            await session.execute(
                select(UserSettingsDocument).where(
                    UserSettingsDocument.user_id.in_(user_ids),
                    UserSettingsDocument.namespace == "notification",
                    UserSettingsDocument.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    default = (True, list(settings.assignment_reminder_default_offsets_hours))
    prefs: dict[uuid.UUID, tuple[bool, list[float]]] = dict.fromkeys(
        user_ids, default
    )
    for doc in docs:
        section = (doc.document or {}).get("assignments") or {}
        enabled = bool(section.get("enabled", True))
        prefs[doc.user_id] = (enabled, _offsets_hours(section, default[1]))
    return prefs


def _offsets_hours(section: dict, default: list[float]) -> list[float]:
    """The section's reminder offsets, in hours.

    Spec §4.6: `reminder_offsets_minutes` is the complete, authoritative
    set -- sub-hour offsets included -- whenever the document carries it,
    and an empty list there means the user turned every offset off.
    `reminder_offsets_hours` (whole hours only) is read only when it does
    not, which is all a client older than 2.1.0 writes. A value that is not
    a list counts as not carrying the field; for `null` that is also what
    the iOS reader (`NotificationSettingsSync.resolveOffsets`) does. Both
    lists drop elements that are not numbers.
    """
    minutes = section.get("reminder_offsets_minutes")
    if isinstance(minutes, list):
        return [value / 60 for value in _numbers(minutes)]
    hours = section.get("reminder_offsets_hours")
    if isinstance(hours, list):
        return _numbers(hours)
    return default


def _numbers(raw: list) -> list[float]:
    return [float(value) for value in raw if isinstance(value, (int, float))]


async def scan_assignment_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One scan pass. Returns the number of push_jobs created."""
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=settings.assignment_reminder_window_hours)
    created = 0
    cancelled = 0
    upgraded = 0

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(UserAssignment, UserAssignmentOverride)
                .outerjoin(
                    UserAssignmentOverride,
                    UserAssignmentOverride.user_assignment_id == UserAssignment.id,
                )
                .where(
                    UserAssignment.deleted_at.is_(None),
                    UserAssignment.provider_is_submitted.is_(False),
                    UserAssignment.due_at.is_not(None),
                    UserAssignment.due_at > now,
                    UserAssignment.due_at <= window_end,
                )
            )
        ).all()

        eligible = [
            assignment
            for assignment, override in rows
            if override is None
            or override.local_status not in _EXCLUDED_LOCAL_STATUSES
        ]
        user_ids = {a.user_id for a in eligible}

        # Pending reminder jobs of EVERY user — also covers users whose
        # last eligible assignment just got submitted/deleted. The set is
        # bounded by outstanding (not yet fired) reminders, which the
        # cancellation pass must examine anyway. SKIP LOCKED: a row the
        # pipeline is concurrently claiming must not be cancelled here —
        # the scan would overwrite its `processing` status after the
        # claim commits and silently drop the notification.
        pending_jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(
                        PushJob.channel == CHANNEL,
                        PushJob.status == PushJobStatus.pending.value,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        user_ids |= {j.user_id for j in pending_jobs}
        if not user_ids:
            return 0

        prefs = await _notification_prefs(session, user_ids, settings)

        # Scoped by user: dedupe_key alone is NOT globally unique (the
        # push_jobs index is (user_id, dedupe_key)) — two users sharing a
        # Moodle assignment produce the same key string, and user A's
        # still-eligible key must not keep user B's stale job alive.
        valid_keys: set[tuple[uuid.UUID, str]] = set()
        values: list[dict] = []
        for assignment in eligible:
            enabled, offsets = prefs[assignment.user_id]
            if not enabled:
                continue
            due_epoch = int(assignment.due_at.timestamp())
            for offset in offsets:
                fire_at = assignment.due_at - timedelta(hours=offset)
                key = _dedupe_key(
                    assignment.moodle_assignment_id, offset, due_epoch
                )
                # Valid even when fire_at has passed — an already-due
                # pending job must not be cancelled mid-delivery.
                valid_keys.add((assignment.user_id, key))
                if fire_at <= now:
                    continue
                values.append(
                    {
                        "user_id": assignment.user_id,
                        "dedupe_key": key,
                        "channel": CHANNEL,
                        "scenario": f"reminder_{_fmt_offset(offset)}h",
                        "fire_at": fire_at,
                        "payload": {
                            "kind": "assignment_reminder",
                            # Copy inputs, not copy: the title and body are
                            # written per recipient at send time, in each
                            # device's language (`push/notification_copy.py`).
                            "assignment_title": assignment.title,
                            "course_name": assignment.course_name,
                            "moodle_assignment_id": assignment.moodle_assignment_id,
                            "moodle_course_id": assignment.moodle_course_id,
                            "due_at": assignment.due_at.isoformat(),
                            "due_epoch": due_epoch,
                            # No moodle_url: client-uploaded URLs are
                            # untrusted (could embed session params); the
                            # client deep-links from the moodle ids.
                            "offset_hours": offset,
                        },
                    }
                )

        if values:
            result = await session.execute(
                pg_insert(PushJob).values(values).on_conflict_do_nothing()
            )
            created = result.rowcount or 0

        # A job filed before copy moved to send time holds a finished
        # Chinese title and body instead of the copy inputs, and the insert
        # above leaves it alone (same key). Each one still waiting gets the
        # payload it would be filed with today, so it too goes out in its
        # recipient's language. One already due has no entry here and goes
        # out as filed.
        fresh_payloads = {
            (value["user_id"], value["dedupe_key"]): value["payload"]
            for value in values
        }
        for job in pending_jobs:
            key = (job.user_id, job.dedupe_key)
            if key not in valid_keys:
                job.status = PushJobStatus.cancelled.value
                job.cancelled_at = now
                cancelled += 1
            elif key in fresh_payloads and not has_reminder_copy_inputs(
                job.payload or {}
            ):
                job.payload = dict(fresh_payloads[key])
                upgraded += 1

    if created or cancelled or upgraded:
        logger.info(
            "push.reminders.scan",
            created=created,
            cancelled=cancelled,
            upgraded=upgraded,
        )
    return created
