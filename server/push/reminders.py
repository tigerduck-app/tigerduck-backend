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


def _dedupe_key(moodle_assignment_id: int, offset: float, due_epoch: int) -> str:
    return (
        f"assignment:moodle:{moodle_assignment_id}:"
        f"reminder_{_fmt_offset(offset)}h:{due_epoch}"
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
        raw = section.get("reminder_offsets_hours")
        offsets = (
            [float(value) for value in raw if isinstance(value, (int, float))]
            if isinstance(raw, list)
            else default[1]
        )
        prefs[doc.user_id] = (enabled, offsets)
    return prefs


async def scan_assignment_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One scan pass. Returns the number of push_jobs created."""
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=settings.assignment_reminder_window_hours)
    created = 0
    cancelled = 0

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
        # last eligible assignment just got submitted/deleted.
        pending_jobs = (
            (
                await session.execute(
                    select(PushJob).where(
                        PushJob.channel == CHANNEL,
                        PushJob.status == PushJobStatus.pending.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        user_ids |= {j.user_id for j in pending_jobs}
        if not user_ids:
            return 0

        prefs = await _notification_prefs(session, user_ids, settings)

        valid_keys: set[str] = set()
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
                valid_keys.add(key)
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
                            "title": f"作業提醒：{assignment.title}",
                            "body": (
                                f"{assignment.course_name + ' · ' if assignment.course_name else ''}"
                                f"剩 {_fmt_offset(offset)} 小時"
                            ),
                            "moodle_assignment_id": assignment.moodle_assignment_id,
                            "moodle_course_id": assignment.moodle_course_id,
                            "due_at": assignment.due_at.isoformat(),
                            "due_epoch": due_epoch,
                            "moodle_url": assignment.moodle_url,
                            "offset_hours": offset,
                        },
                    }
                )

        if values:
            result = await session.execute(
                pg_insert(PushJob).values(values).on_conflict_do_nothing()
            )
            created = result.rowcount or 0

        for job in pending_jobs:
            if job.dedupe_key in valid_keys:
                continue
            job.status = PushJobStatus.cancelled.value
            job.cancelled_at = now
            cancelled += 1

    if created or cancelled:
        logger.info("push.reminders.scan", created=created, cancelled=cancelled)
    return created
