"""Phase 4b: generate course start reminder push_jobs.

`user_courses.schedule_json` is what the apps upload,
`{"<ISO weekday 1-7>": [<NTUST period>, ...]}`, or the older list of
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
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Row, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.changes import load_course_changes, meeting_changes
from server.academic_calendar.models import AcademicHoliday, UserHolidayOverride
from server.academic_calendar.terms import (
    current_term_code,
    is_term_code,
    load_term_dates,
    outside_term,
)
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
from server.db import in_array, session_scope
from server.push.reminders import file_jobs
from server.sync.models import (
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)

logger = structlog.get_logger(__name__)

_ISO_WEEKDAYS = range(1, 8)
_WEEKDAY_KEYS = {str(day) for day in _ISO_WEEKDAYS}
# NTUST's period order; periods next to each other in it make one meeting, as in the apps.
_PERIOD_RANK = {
    period: rank
    for rank, period in enumerate((*(str(n) for n in range(1, 11)), "A", "B", "C", "D"))
}

CHANNEL = "course"


def _entry_start_time(
    entry: object, period_times: dict[str, str]
) -> tuple[int, time, str] | None:
    """Return (iso_weekday, local_start_time, first_period) for one schedule
    entry, or None when the entry is malformed / has no known period."""
    if not isinstance(entry, dict):
        return None
    day = entry.get("day")
    if not isinstance(day, int) or day not in _ISO_WEEKDAYS:
        return None
    starts: list[tuple[time, str]] = []
    periods = entry.get("periods")
    if not isinstance(periods, list):
        return None
    for period in periods:
        raw = period_times.get(str(period))
        if raw is None:
            continue
        hour, _, minute = raw.partition(":")
        try:
            starts.append((time(int(hour), int(minute)), str(period)))
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
    return day, *min(starts)


def _schedule_entries(schedule_json: object) -> list:
    """The schedule as list entries. A weekday's periods in the object form
    are one entry per run of consecutive periods, the meetings the portal
    shows; periods NTUST does not have are left out."""
    if isinstance(schedule_json, list):
        return schedule_json
    if not isinstance(schedule_json, dict):
        return []
    entries: list[dict] = []
    for day, periods in schedule_json.items():
        if day not in _WEEKDAY_KEYS or not isinstance(periods, list):
            continue
        run: list[str] = []
        for period in sorted({str(p) for p in periods if str(p) in _PERIOD_RANK}, key=_PERIOD_RANK.get):
            if run and _PERIOD_RANK[period] != _PERIOD_RANK[run[-1]] + 1:
                entries.append({"day": int(day), "periods": run})
                run = []
            run.append(period)
        if run:
            entries.append({"day": int(day), "periods": run})
    return entries


def course_occurrences(
    schedule_json: list | dict,
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
    return [
        occurrence
        for occurrence, _ in class_meetings(
            schedule_json,
            window_start=window_start,
            window_end=window_end,
            tz=tz,
            period_times=period_times,
        )
    ]


def class_meetings(
    schedule_json: list | dict,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    period_times: dict[str, str],
) -> list[tuple[datetime, str]]:
    """`course_occurrences`, each with the period it starts in."""
    slots = [
        slot
        for entry in _schedule_entries(schedule_json)
        if (slot := _entry_start_time(entry, period_times)) is not None
    ]
    if not slots:
        return []

    occurrences: dict[datetime, str] = {}
    local_date = window_start.astimezone(tz).date()
    end_date = window_end.astimezone(tz).date()
    while local_date <= end_date:
        weekday = local_date.isoweekday()
        for day, start, period in slots:
            if day != weekday:
                continue
            occurrence = datetime.combine(local_date, start, tzinfo=tz)
            if window_start < occurrence <= window_end:
                occurrences.setdefault(occurrence.astimezone(window_start.tzinfo), period)
        local_date += timedelta(days=1)
    return sorted(occurrences.items())


def changed_meeting(
    changes: Sequence[Row], course: UserCourse, occurrence: datetime, period: str, tz: tzinfo
) -> tuple[datetime, str | None] | None:
    """The start and room of the meeting of `course` that starts in `period`
    at `occurrence`, as the school's course changes leave them; None when it
    is cancelled."""
    day = occurrence.astimezone(tz).date()
    changed = (
        meeting_changes(
            changes, term=course.semester, course_no=course.course_no, day=day, period=period
        )
        if course.course_no
        else {}
    )
    if "cancelled" in changed:
        return None
    if "start" in changed:
        hour, minute = changed["start"].start_time.split(":")
        occurrence = datetime.combine(day, time(int(hour), int(minute)), tzinfo=tz)
    room = changed["room"].room if "room" in changed else course.classroom
    return occurrence.astimezone(UTC), room


def _reminder_body(start: datetime, room: str | None, tz: tzinfo) -> str:
    # The start time and room, as Android's own class reminder shows them; the
    # same in every language.
    return " · ".join(part for part in (f"{start.astimezone(tz):%H:%M}", room) if part)


async def recheck_class_reminder(
    session: AsyncSession, payload: dict, *, tz: tzinfo, period_times: dict[str, str]
) -> dict | None:
    """The class reminder's payload as its course and the school's course
    changes stand now, or None when its class no longer starts at the time
    it was filed for: cancelled, moved, or the course gone.

    The pipeline asks right before the first delivery; a moved class has a
    reminder of its own from the scan. A job filed before copy moved to send
    time keeps its own body.
    """
    user_course_id = payload.get("user_course_id")
    start_epoch = payload.get("start_epoch")
    if type(user_course_id) is not int or type(start_epoch) is not int:
        return None
    course = await session.get(UserCourse, user_course_id)
    if course is None:
        return None
    start = datetime.fromtimestamp(start_epoch, UTC)
    midnight = datetime.combine(start.astimezone(tz).date(), time(0), tzinfo=tz)
    changes = (
        await load_course_changes(session, [course.semester], course_no=course.course_no)
        if course.course_no
        else []
    )
    for occurrence, period in class_meetings(
        course.schedule_json,
        window_start=midnight.astimezone(UTC),
        window_end=midnight.astimezone(UTC) + timedelta(days=1),
        tz=tz,
        period_times=period_times,
    ):
        meeting = changed_meeting(changes, course, occurrence, period, tz)
        if meeting is not None and meeting[0] == start:
            if "course_name" not in payload:
                return payload
            return {**payload, "body": _reminder_body(*meeting, tz)}
    return None


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
                    in_array(UserSettingsDocument.user_id, user_ids),
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
        # Absent means on; anything but a JSON boolean is malformed and counts
        # as off (#104).
        enabled = section.get("enabled", True) is True
        raw = section.get("reminder_offsets_minutes")
        offsets = (
            [
                float(value)
                for value in raw
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            if isinstance(raw, list)
            else default[1]
        )
        prefs[doc.user_id] = (enabled, offsets)
    return prefs


async def _users_with_a_recipient(
    session: AsyncSession,
    user_ids: set[uuid.UUID],
    now: datetime,
    *,
    token_required: bool = True,
) -> set[uuid.UUID]:
    """The users among `user_ids` with at least one device the pipeline
    would deliver a class reminder to: an active standard token on an
    iPhone or iPad. With `token_required=False`, the iPhone or iPad alone,
    whatever its tokens.

    Mirrors `reminders._users_with_a_recipient`, for the same reason. The
    pipeline checks again at send time and stays the authority; this only
    keeps the scan from filing jobs nothing could receive. Filed anyway,
    every reminder of an Android-only account would settle as
    `failed`/`no_active_tokens`, a failed row per reminder.
    """
    if not user_ids:
        return set()
    query = select(UserDevice.user_id).where(
        in_array(UserDevice.user_id, user_ids),
        UserDevice.deleted_at.is_(None),
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
    rows = await session.execute(query.distinct())
    return set(rows.scalars().all())


async def _current_terms(
    session: AsyncSession, window_start: datetime, window_end: datetime, tz: tzinfo
) -> dict[date, str | None]:
    """Each local day of the window, mapped to the term both apps take as
    current on it (`current_term_code`)."""
    by_day: dict[date, str | None] = {}
    day = window_start.astimezone(tz).date()
    while day <= window_end.astimezone(tz).date():
        # Noon keeps the day the same in the school's zone.
        by_day[day] = await current_term_code(
            session, datetime.combine(day, time(12), tzinfo=tz)
        )
        day += timedelta(days=1)
    return by_day


async def scan_course_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One scan pass. Returns the number of push_jobs created.

    Eligible courses: not deleted, enrolled, non-empty schedule, not hidden
    by override, and of the term both apps show on the occurrence's day
    (`current_term_code`); with no term published, of the user's LATEST
    semester (lexicographic max of "1141"-style strings — older semesters
    must not keep firing weekly reminders). Occurrence dates listed in
    user_course_skipped_dates are excluded, and so are days no published
    term runs on (`outside_term`). The school's course changes apply
    (`changed_meeting`): a cancelled class files nothing, a new start moves
    the reminder and a new room reaches its body. Cancellation mirrors
    assignment reminders: pending jobs whose dedupe key no longer maps to an
    eligible (course, occurrence, offset) tuple are cancelled, under SKIP
    LOCKED so a job the pipeline is claiming concurrently is left alone.

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
    # From the start of today: a class that began may have a later start today.
    day_start = datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz)
    period_times = settings.course_period_start_times
    created = 0
    cancelled = 0

    async with session_scope(session_factory) as session:
        # The term whose courses meet on each day of the window: the one
        # both apps show that day. The next term's courses, synced weeks
        # ahead once course selection serves them, wait for it to begin,
        # and the term before keeps its own until then.
        terms = await load_term_dates(session)
        term_on = await _current_terms(session, now, window_end, tz) if terms else {}

        query = (
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
        if term_on:
            # Every occurrence is held to its day's term and in-session check
            # below, so no other term's course could file or keep a reminder
            # (https://github.com/tigerduck-app/tigerduck-backend/issues/95).
            query = query.where(
                UserCourse.semester.in_(
                    [
                        code
                        for day, code in term_on.items()
                        if not outside_term(day, terms)
                    ]
                )
            )
        rows = (await session.execute(query)).all()

        # Latest semester per user, which stands in for the term while none
        # is published. It prefers portal-sourced courses and `user_added`
        # ones filed under a term code, as the course upload files a course
        # added by hand. The initial upload takes any key and semester, so a
        # `user_added` row can carry a free-form semester string that could
        # lexicographically outrank the real current semester (e.g. "9999")
        # and silently mute every portal course's reminders.
        # Two buckets merged portal-last so the result is independent of
        # row order (a single-pass flag was order-sensitive).
        portal_latest: dict[uuid.UUID, str] = {}
        fallback_latest: dict[uuid.UUID, str] = {}
        for course, _override in rows:
            bucket = (
                portal_latest
                if course.source == "ntust_portal" or is_term_code(course.semester)
                else fallback_latest
            )
            current = bucket.get(course.user_id)
            if current is None or course.semester > current:
                bucket[course.user_id] = course.semester
        latest_semester = {**fallback_latest, **portal_latest}

        # Days classes do not meet: no published term runs on them, or a
        # holiday the user did not put back covers them. Apple guards twice
        # — the app also stays quiet — but the backend half is what stops a
        # push being *sent*, which is the only half that works when the app
        # is not running to suppress anything. The class Live Activity
        # starts apply the same rule when they fire —
        # `pipeline._classes_quiet`; a change here belongs there too.
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

        # Removed courses are hard-deleted, so any course still present is
        # eligible.
        if term_on:
            eligible = rows
        else:
            eligible = [
                (course, override)
                for course, override in rows
                if course.semester == latest_semester[course.user_id]
            ]
        user_ids = {course.user_id for course, _ in eligible}
        changes = await load_course_changes(
            session, {course.semester for course, _ in eligible}
        )

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
        eligible_users = {course.user_id for course, _ in eligible}
        reachable = await _users_with_a_recipient(session, eligible_users, now)
        # An iPhone or iPad with no usable token right now: APNs dropped
        # it, or the app has not registered a new one yet. Nothing new is
        # filed for these users, but their filed reminders stay. Cancelled,
        # one due before the next scan was gone for good even when the app
        # registered a token in time, since a scan only files future jobs.
        # Still unreachable when due, it fails as `no_active_tokens`.
        awaiting_token = await _users_with_a_recipient(
            session, eligible_users - reachable, now, token_required=False
        )

        skipped: set[tuple[int, date]] = set()
        course_ids = [course.id for course, _ in eligible]
        if course_ids:
            skip_rows = (
                await session.execute(
                    select(
                        UserCourseSkippedDate.user_course_id,
                        UserCourseSkippedDate.skipped_on,
                    ).where(
                        in_array(UserCourseSkippedDate.user_course_id, course_ids),
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
            # No device could ever receive it (Android only, or signed
            # out). Its pending jobs drop out of valid_keys and are
            # cancelled below, as when reminders are switched off, and are
            # filed again once a device can.
            if not enabled or (
                course.user_id not in reachable
                and course.user_id not in awaiting_token
            ):
                continue
            meetings = class_meetings(
                course.schedule_json,
                window_start=day_start.astimezone(UTC),
                window_end=window_end,
                tz=tz,
                period_times=period_times,
            )
            display_name = (
                override.custom_name
                if override is not None and override.custom_name
                else course.course_name
            )
            for occurrence, period in meetings:
                occurrence_day = occurrence.astimezone(tz).date()
                if (course.id, occurrence_day) in skipped:
                    continue
                if term_on and term_on[occurrence_day] != course.semester:
                    continue
                # "還要上課？" puts back a holiday, never a day outside the term.
                if outside_term(occurrence_day, terms):
                    continue
                if is_quiet(course.user_id, occurrence_day):
                    continue
                meeting = changed_meeting(changes, course, occurrence, period, tz)
                if meeting is None:
                    continue
                occurrence, room = meeting
                # Begun, so it files nothing and its pending reminders go below.
                if occurrence <= now:
                    continue
                start_epoch = int(occurrence.timestamp())
                body = _reminder_body(occurrence, room, tz)
                for offset in offsets:
                    fire_at = occurrence - timedelta(minutes=offset)
                    key = _dedupe_key(course.id, offset, start_epoch)
                    # Valid even when fire_at has passed — an already-due
                    # pending job must not be cancelled mid-delivery.
                    valid_keys.add((course.user_id, key))
                    if fire_at <= now:
                        continue
                    values.append(
                        {
                            "user_id": course.user_id,
                            "dedupe_key": key,
                            "channel": CHANNEL,
                            "scenario": f"reminder_{_fmt_offset(offset)}m",
                            "fire_at": fire_at,
                            "payload": {
                                "kind": "course_reminder",
                                # The title is written in each recipient's
                                # language at send time
                                # (`notification_copy.build_course_reminder_payload`).
                                "course_name": display_name,
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

        # Filed only where a device can receive it now.
        to_file = [value for value in values if value["user_id"] in reachable]
        created = await file_jobs(session, to_file)

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
