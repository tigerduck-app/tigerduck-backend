"""Phase 4b: generate course start reminder push_jobs.

`user_courses.schedule_json` is a client-uploaded list of
`{"day": <ISO weekday 1-7>, "periods": [<NTUST period>, ...]}` entries.
The server projects each entry onto concrete dates inside the scan
window using the configured period→start-time table (Asia/Taipei by
default), then mints one push_job per (occurrence, offset) with the
occurrence epoch embedded in the dedupe key — same durability story as
assignment reminders: a schedule change creates NEW keys and the scan
cancels pending jobs whose key is no longer valid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.models import AcademicHoliday, UserHolidayOverride
from server.auth.models import PushJob, PushJobStatus
from server.config import Settings
from server.db import session_scope
from server.sync.models import (
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)

logger = structlog.get_logger(__name__)

_ISO_WEEKDAYS = range(1, 8)

CHANNEL = "course"


def _entry_start_time(
    entry: object, period_times: dict[str, str]
) -> tuple[int, time] | None:
    """Return (iso_weekday, local_start_time) for one schedule entry, or
    None when the entry is malformed / has no known period."""
    if not isinstance(entry, dict):
        return None
    day = entry.get("day")
    if not isinstance(day, int) or day not in _ISO_WEEKDAYS:
        return None
    starts: list[time] = []
    periods = entry.get("periods")
    if not isinstance(periods, list):
        return None
    for period in periods:
        raw = period_times.get(str(period))
        if raw is None:
            continue
        hour, _, minute = raw.partition(":")
        try:
            starts.append(time(int(hour), int(minute)))
        except ValueError:
            # Malformed operator config ("HH:MM" expected) must degrade to
            # "no reminder for this period", not crash the whole scan tick.
            logger.warning(
                "push.course_reminders.bad_period_time",
                period=str(period),
                value=raw,
            )
            continue
    if not starts:
        return None
    return day, min(starts)


def course_occurrences(
    schedule_json: list,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    period_times: dict[str, str],
) -> list[datetime]:
    """Concrete UTC class-block start times within (window_start, window_end].

    One occurrence per (entry, matching date); a multi-period entry is one
    teaching block starting at its earliest period. Malformed entries and
    unknown periods are skipped. Result is sorted and de-duplicated.
    """
    slots = [
        slot
        for entry in schedule_json
        if (slot := _entry_start_time(entry, period_times)) is not None
    ]
    if not slots:
        return []

    occurrences: set[datetime] = set()
    local_date = window_start.astimezone(tz).date()
    end_date = window_end.astimezone(tz).date()
    while local_date <= end_date:
        weekday = local_date.isoweekday()
        for day, start in slots:
            if day != weekday:
                continue
            occurrence = datetime.combine(local_date, start, tzinfo=tz)
            if window_start < occurrence <= window_end:
                occurrences.add(occurrence.astimezone(window_start.tzinfo))
        local_date += timedelta(days=1)
    return sorted(occurrences)


def _fmt_offset(minutes: float) -> str:
    return f"{minutes:g}"


def _dedupe_key(user_course_id: int, offset: float, start_epoch: int) -> str:
    return (
        f"course:{user_course_id}:reminder_{_fmt_offset(offset)}m:{start_epoch}"
    )


async def _course_prefs(
    session: AsyncSession, user_ids: set[uuid.UUID], settings: Settings
) -> dict[uuid.UUID, tuple[bool, list[float]]]:
    """user_id → (enabled, offsets_minutes). Missing doc → server defaults.

    Same shape as `reminders._notification_prefs` but reads the `courses`
    section; the shared-default-tuple pattern is safe because entries are
    replaced wholesale, never mutated.
    """
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
    default = (True, list(settings.course_reminder_default_offsets_minutes))
    prefs: dict[uuid.UUID, tuple[bool, list[float]]] = dict.fromkeys(
        user_ids, default
    )
    for doc in docs:
        section = (doc.document or {}).get("courses") or {}
        enabled = bool(section.get("enabled", True))
        raw = section.get("reminder_offsets_minutes")
        offsets = (
            [float(value) for value in raw if isinstance(value, (int, float))]
            if isinstance(raw, list)
            else default[1]
        )
        prefs[doc.user_id] = (enabled, offsets)
    return prefs


async def scan_course_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One scan pass. Returns the number of push_jobs created.

    Eligible courses: not deleted, enrolled, non-empty schedule, in the
    user's LATEST semester (lexicographic max of "1141"-style strings —
    older semesters must not keep firing weekly reminders), not hidden by
    override. Occurrence dates listed in user_course_skipped_dates are
    excluded. Cancellation mirrors assignment reminders: pending jobs
    whose dedupe key no longer maps to an eligible (course, occurrence,
    offset) tuple are cancelled, under SKIP LOCKED so a job the pipeline
    is claiming concurrently is left alone.

    Two deliberate semantics worth naming:
    * The global pending scan is unbounded by user but bounded by
      outstanding (not yet fired) course reminders — the cancellation
      pass must examine all of them anyway (same call as 4a reminders).
    * Once an occurrence's start time has PASSED, its keys drop out of
      valid_keys and a still-pending job is cancelled: a "10 分鐘後上課"
      reminder delivered after class started is worse than none. (4a
      keeps past-fire keys valid; there the assignment is still due.)
    * Occurrence math assumes a fixed-offset zone (Asia/Taipei has no
      DST). A DST-observing `course_reminder_timezone` would be off by
      the fold during transitions.
    """
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=settings.course_reminder_window_hours)
    tz = ZoneInfo(settings.course_reminder_timezone)
    period_times = settings.course_period_start_times
    created = 0
    cancelled = 0

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(UserCourse, UserCourseOverride)
                .outerjoin(
                    UserCourseOverride,
                    UserCourseOverride.user_course_id == UserCourse.id,
                )
                .where(
                    UserCourse.enrollment_status == "enrolled",
                    UserCourse.schedule_json != text("'{}'::jsonb"),
                    UserCourse.schedule_json != text("'[]'::jsonb"),
                )
            )
        ).all()

        # Latest semester per user, preferring portal-sourced courses:
        # `user_added` rows carry a free-form client semester string that
        # could lexicographically outrank the real current semester (e.g.
        # "9999") and silently mute every portal course's reminders.
        # Two buckets merged portal-last so the result is independent of
        # row order (final review: a single-pass flag was order-sensitive).
        portal_latest: dict[uuid.UUID, str] = {}
        fallback_latest: dict[uuid.UUID, str] = {}
        for course, _override in rows:
            bucket = (
                portal_latest
                if course.source == "ntust_portal"
                else fallback_latest
            )
            current = bucket.get(course.user_id)
            if current is None or course.semester > current:
                bucket[course.user_id] = course.semester
        latest_semester = {**fallback_latest, **portal_latest}

        # Days classes do not meet, and the per-user exceptions that put
        # them back. Apple guards twice — the app also stays quiet — but the
        # backend half is what stops a push being *sent*, which is the only
        # half that works when the app is not running to suppress anything.
        holiday_ranges = (
            await session.execute(
                select(
                    AcademicHoliday.id,
                    AcademicHoliday.start_date,
                    AcademicHoliday.end_date,
                )
            )
        ).all()
        opted_in: dict[uuid.UUID, set[int]] = {}
        for user_id, holiday_id in (
            await session.execute(
                select(
                    UserHolidayOverride.user_id, UserHolidayOverride.holiday_id
                ).where(UserHolidayOverride.notify.is_(True))
            )
        ).all():
            opted_in.setdefault(user_id, set()).add(holiday_id)

        def is_quiet(user_id: uuid.UUID, day) -> bool:
            covering = [
                hid for hid, start, end in holiday_ranges if start <= day <= end
            ]
            if not covering:
                return False
            # Opting in to any covering holiday un-suppresses the day: the
            # user said "I have class", and a second overlapping holiday
            # they never saw should not overrule that.
            return not (opted_in.get(user_id, set()) & set(covering))

        eligible = [
            (course, override)
            for course, override in rows
            if course.semester == latest_semester[course.user_id]
            # Removed courses are hard-deleted, so any course still
            # present is eligible.
        ]
        user_ids = {course.user_id for course, _ in eligible}

        # SKIP LOCKED: don't fight the pipeline's claim — see
        # reminders.scan_assignment_reminders for the race rationale.
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
        user_ids |= {job.user_id for job in pending_jobs}
        if not user_ids:
            return 0

        prefs = await _course_prefs(session, user_ids, settings)

        skipped: set[tuple[int, date]] = set()
        course_ids = [course.id for course, _ in eligible]
        if course_ids:
            skip_rows = (
                await session.execute(
                    select(
                        UserCourseSkippedDate.user_course_id,
                        UserCourseSkippedDate.skipped_on,
                    ).where(
                        UserCourseSkippedDate.user_course_id.in_(course_ids),
                        UserCourseSkippedDate.deleted_at.is_(None),
                    )
                )
            ).all()
            skipped = {(row.user_course_id, row.skipped_on) for row in skip_rows}

        # Scoped by user for symmetry with the assignment scan. Course
        # keys embed the globally-unique user_course_id so the bare key
        # would already be safe — but keeping the (user_id, key) shape
        # stops anyone copying this pattern into a scan where it isn't.
        valid_keys: set[tuple[uuid.UUID, str]] = set()
        values: list[dict] = []
        for course, override in eligible:
            enabled, offsets = prefs[course.user_id]
            if not enabled:
                continue
            occurrences = course_occurrences(
                course.schedule_json,
                window_start=now,
                window_end=window_end,
                tz=tz,
                period_times=period_times,
            )
            display_name = (
                override.custom_name
                if override is not None and override.custom_name
                else course.course_name
            )
            for occurrence in occurrences:
                occurrence_day = occurrence.astimezone(tz).date()
                if (course.id, occurrence_day) in skipped:
                    continue
                if is_quiet(course.user_id, occurrence_day):
                    continue
                start_epoch = int(occurrence.timestamp())
                for offset in offsets:
                    fire_at = occurrence - timedelta(minutes=offset)
                    key = _dedupe_key(course.id, offset, start_epoch)
                    # Valid even when fire_at has passed — an already-due
                    # pending job must not be cancelled mid-delivery.
                    valid_keys.add((course.user_id, key))
                    if fire_at <= now:
                        continue
                    body = f"{_fmt_offset(offset)} 分鐘後上課"
                    if course.classroom:
                        body += f" · {course.classroom}"
                    values.append(
                        {
                            "user_id": course.user_id,
                            "dedupe_key": key,
                            "channel": CHANNEL,
                            "scenario": f"reminder_{_fmt_offset(offset)}m",
                            "fire_at": fire_at,
                            "payload": {
                                "kind": "course_reminder",
                                "title": f"上課提醒：{display_name}",
                                "body": body,
                                "semester": course.semester,
                                "course_key": course.course_key,
                                "user_course_id": course.id,
                                "starts_at": occurrence.isoformat(),
                                "start_epoch": start_epoch,
                                "offset_minutes": offset,
                            },
                        }
                    )

        if values:
            result = await session.execute(
                pg_insert(PushJob).values(values).on_conflict_do_nothing()
            )
            created = result.rowcount or 0

        for job in pending_jobs:
            if (job.user_id, job.dedupe_key) in valid_keys:
                continue
            job.status = PushJobStatus.cancelled.value
            job.cancelled_at = now
            cancelled += 1

    if created or cancelled:
        logger.info(
            "push.course_reminders.scan", created=created, cancelled=cancelled
        )
    return created
