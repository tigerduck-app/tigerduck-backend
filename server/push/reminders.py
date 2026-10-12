"""Phase 4a: generate assignment due-date reminder push_jobs.

Scan-based (default every 300s): eligible = not deleted, not submitted,
local_status not in (locally_completed/ignored/archived), not in a course
whose assignments the user ignores (`_ignored_courses`), due within the
scan window, the user's `notification` settings document has
assignments enabled (no document → enabled with the server default
offsets), and the user has a device the pipeline would deliver the
reminder to (`_users_with_a_recipient`). One push_job per (assignment,
offset) with the due-date epoch embedded in the dedupe key —
`ux_push_jobs_dedupe_active` also covers sent states (security fix 1.2),
so a due-date change must mint a NEW key; the scan cancels pending jobs
whose key is no longer valid (due changed, assignment completed/ignored/
deleted, its course's assignments ignored, reminders disabled, or no
device left to receive them).

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

from server.auth.models import (
    APPLE_HANDHELD_PLATFORMS,
    DevicePushToken,
    PushJob,
    PushJobStatus,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.config import Settings
from server.db import session_scope
from server.push.client_versions import schedules_reminders_locally
from server.push.notification_copy import has_reminder_copy_inputs
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
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


async def _ignored_courses(
    session: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[set[int], set[str]]]:
    """user_id → (numeric Moodle course ids, course numbers) of the courses
    whose assignments the user ignores (`ignore_assignments`).

    The server fetches assignments itself and keys them by the numeric
    course id, which the override holds once a client has sent it. A row a
    client uploaded is a placeholder with course id 0 that carries the
    course number instead, so only placeholders are matched by number. A
    course number is the same in every semester: the placeholder of a course
    taken again after it was ignored is skipped too, until the server's own
    fetch soft-deletes the placeholder.
    """
    ignored: dict[uuid.UUID, tuple[set[int], set[str]]] = {}
    if not user_ids:
        return ignored
    rows = await session.execute(
        select(
            UserCourseOverride.user_id,
            UserCourseOverride.moodle_course_id,
            UserCourse.course_no,
        )
        .join(UserCourse, UserCourse.id == UserCourseOverride.user_course_id)
        .where(
            UserCourseOverride.user_id.in_(user_ids),
            UserCourseOverride.ignore_assignments.is_(True),
        )
    )
    for user_id, moodle_course_id, course_no in rows:
        course_ids, course_nos = ignored.setdefault(user_id, (set(), set()))
        if moodle_course_id is not None:
            course_ids.add(moodle_course_id)
        if course_no:
            course_nos.add(course_no)
    return ignored


def _in_ignored_course(
    assignment: UserAssignment,
    ignored: dict[uuid.UUID, tuple[set[int], set[str]]],
) -> bool:
    course_ids, course_nos = ignored.get(assignment.user_id, (set(), set()))
    if assignment.moodle_course_id in course_ids:
        return True
    return assignment.moodle_course_id == 0 and assignment.course_no in course_nos


async def _users_with_a_recipient(
    session: AsyncSession,
    user_ids: set[uuid.UUID],
    now: datetime,
    *,
    token_required: bool = True,
) -> set[uuid.UUID]:
    """The users among `user_ids` with at least one device the pipeline
    would deliver an assignment reminder to. With `token_required=False`,
    a device that passes every condition but the token.

    The conditions `push.pipeline._materialize` applies on this channel: an
    iPhone or iPad with both sync switches on, an app new enough not to
    schedule its own reminders, and an active standard token. The pipeline
    checks them again at send time and stays the authority, since a device
    can change before the fire time; this only keeps the scan from filing
    jobs nothing could receive. Every Android-only account is one (Android
    arms these reminders itself), as is every iPhone still on a build that
    schedules its own. Filed anyway, each reminder would settle as
    `failed`/`no_active_tokens` once due, a failed row per reminder.
    """
    if not user_ids:
        return set()
    query = select(
        UserDevice.user_id, UserDevice.platform, UserDevice.app_version
    ).where(
        UserDevice.user_id.in_(user_ids),
        UserDevice.deleted_at.is_(None),
        UserDevice.cloud_sync_enabled.is_(True),
        UserDevice.sync_assignment_reminders.is_(True),
        UserDevice.platform.in_(APPLE_HANDHELD_PLATFORMS),
    )
    if token_required:
        query = query.join(
            DevicePushToken, DevicePushToken.device_id == UserDevice.id
        ).where(
            DevicePushToken.token_kind == PushTokenKind.standard.value,
            DevicePushToken.status == PushTokenStatus.active.value,
            (DevicePushToken.expires_at.is_(None))
            | (DevicePushToken.expires_at > now),
        )
    rows = (await session.execute(query)).all()
    # The version gate stays in Python, as in the pipeline: app_version is
    # VARCHAR, and "2.10.0" sorts below "2.9.0" as a string.
    return {
        user_id
        for user_id, platform, app_version in rows
        if not schedules_reminders_locally(platform, app_version)
    }


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

        not_marked = [
            assignment
            for assignment, override in rows
            if override is None
            or override.local_status not in _EXCLUDED_LOCAL_STATUSES
        ]
        ignored = await _ignored_courses(session, {a.user_id for a in not_marked})
        eligible = [a for a in not_marked if not _in_ignored_course(a, ignored)]
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
        eligible_users = {a.user_id for a in eligible}
        reachable = await _users_with_a_recipient(session, eligible_users, now)
        # A device that would get the reminder but has no usable token
        # right now: APNs dropped it, or the app has not registered a new
        # one yet. Nothing new is filed for these users, but their filed
        # reminders stay. Cancelled, one due before the next scan was gone
        # for good even when the app registered a token in time, since a
        # scan only files future jobs. Still unreachable when due, it fails
        # as `no_active_tokens`.
        awaiting_token = await _users_with_a_recipient(
            session, eligible_users - reachable, now, token_required=False
        )

        # Scoped by user: dedupe_key alone is NOT globally unique (the
        # push_jobs index is (user_id, dedupe_key)) — two users sharing a
        # Moodle assignment produce the same key string, and user A's
        # still-eligible key must not keep user B's stale job alive.
        valid_keys: set[tuple[uuid.UUID, str]] = set()
        values: list[dict] = []
        for assignment in eligible:
            enabled, offsets = prefs[assignment.user_id]
            # No device could ever receive it (Android only, sync or
            # reminders switched off, an app that arms its own, signed
            # out). Its pending jobs drop out of valid_keys and are
            # cancelled below, as when reminders are switched off, and are
            # filed again once a device can.
            if not enabled or (
                assignment.user_id not in reachable
                and assignment.user_id not in awaiting_token
            ):
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

        # Filed only where a device can receive it now. `values` still
        # holds the waiting users' reminders, for the payload upgrade below.
        to_file = [value for value in values if value["user_id"] in reachable]
        if to_file:
            result = await session.execute(
                pg_insert(PushJob).values(to_file).on_conflict_do_nothing()
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
