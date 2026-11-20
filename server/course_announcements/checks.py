"""The checks an extracted course change must pass before it may change
`course_changes` without an operator.

The rules a change keeps against the course's class times and its other
changes are the database's `course_change_violations`
(server/academic_calendar/models.py), which the portal calls too; a caller
takes `lock_courses` first, in the transaction that writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date, SmallInteger, Text

from server.academic_calendar.models import CourseChange, SemesterTerm
from server.academic_calendar.terms import TermDates
from server.push.course_reminders import PERIOD_RANK

ROOM_MAX = 128
# The fields of a change besides its days, which tell two changes of a day apart.
VALUE_FIELDS = ("periods", "room", "start_time", "end_time", "note_zh")
_KIND_FIELDS = {
    "room": ("room",),
    "start": ("start_time",),
    "end": ("end_time",),
    "assessment": ("note_zh", "note_en"),
    "online": ("note_zh",),
    "note": ("note_zh",),
}
# Kinds whose fields may be left out.
_OPTIONAL_FIELDS = frozenset({"online"})
# The labels both the classifier and the extraction must give a change of a kind.
_LABELS = {
    "cancelled": frozenset({"class_cancel"}),
    "assessment": frozenset({"exam_info", "exam_change"}),
}
_CLASS_CHANGE = frozenset({"class_change"})
#: The assessment an extracted exam of each kind becomes, named as the
#: portal's presets name it (portal/web/src/pages/course-changes/ChangeDialog.tsx).
EXAM_NAMES = {
    "midterm": ("期中考", "Midterm exam"),
    "final": ("期末考", "Final exam"),
    "quiz": ("小考", "Quiz"),
}

_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_VIOLATIONS = text(
    "SELECT code, day, change_id FROM course_change_violations("
    ":term, :course, :kind, :mode, :first, :last, :excluded, :periods, :editing)"
).bindparams(
    bindparam("first", type_=Date),
    bindparam("last", type_=Date),
    bindparam("excluded", type_=ARRAY(SmallInteger)),
    bindparam("periods", type_=ARRAY(Text)),
)
_NO_MEETINGS = text(
    "SELECT EXISTS (SELECT 1 FROM course_change_violations("
    ":term, :course, NULL, NULL, NULL, NULL, '{}', NULL) WHERE code = 'no_meetings')"
)
_NOT_CLASS_DAY = {"no_meetings", "no_class_day", "holiday"}

# Monday to Sunday.
ZH_WEEKDAYS = "一二三四五六日"
_WEEKDAY_CHARS = {char: n for n, char in enumerate(ZH_WEEKDAYS, start=1)} | {"天": 7}
_ZH_WEEKDAY = re.compile(r"(?:週|周|星期|禮拜)([一二三四五六日天])|[（(]([一二三四五六日])[）)]")
_EN_WEEKDAYS = {
    "mon": 1, "monday": 1, "tue": 2, "tues": 2, "tuesday": 2, "wed": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6, "sun": 7, "sunday": 7,
}
_EN_WEEKDAY = re.compile(
    r"\b(" + "|".join(sorted(_EN_WEEKDAYS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


def change_payload(fact: Mapping[str, Any]) -> dict[str, Any]:
    """An extracted change in `course_changes` field names, with only its
    kind's own value, as the portal reads a change."""
    periods = fact["periods"]
    if periods and set(periods) <= set(PERIOD_RANK):
        periods = sorted(set(periods), key=PERIOD_RANK.__getitem__)
    own = _KIND_FIELDS.get(fact["kind"], ())
    return {
        "kind": fact["kind"],
        "mode": fact["mode"],
        "start_date": fact["start_date"],
        "end_date": fact["end_date"],
        "excluded_weekdays": sorted(set(fact["excluded_weekdays"])),
        "periods": periods,
        **{field: _strip(fact[field]) if field in own else None for field in VALUE_FIELDS[1:]},
        "note_en": None,
    }


def exam_payload(exam: Mapping[str, Any]) -> dict[str, Any]:
    """An extracted exam with a date as an assessment of the course's
    meetings that day, named by its kind (`EXAM_NAMES`); an exam of another
    kind keeps its extracted Chinese name and has no English one, which an
    operator gives it."""
    note_zh, note_en = EXAM_NAMES.get(exam["kind"], (_strip(exam.get("name")), None))
    return {
        "kind": "assessment",
        "mode": "day",
        "start_date": exam["date"],
        "end_date": exam["date"],
        "excluded_weekdays": [],
        "periods": None,
        "room": None,
        "start_time": None,
        "end_time": None,
        "note_zh": note_zh,
        "note_en": note_en,
    }


def _strip(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


def stored_payload(change: CourseChange) -> dict[str, Any]:
    """A stored change in the shape of `change_payload`."""
    return {
        "kind": change.kind,
        "mode": change.mode,
        "start_date": change.start_date.isoformat(),
        "end_date": change.end_date.isoformat(),
        "excluded_weekdays": sorted(change.excluded_weekdays),
        "periods": change.periods,
        **{field: getattr(change, field) for field in VALUE_FIELDS[1:]},
        "note_en": change.note_en,
    }


def days_of(payload: Mapping[str, Any]) -> tuple[date, date]:
    return date.fromisoformat(payload["start_date"]), date.fromisoformat(payload["end_date"])


def covers_day(payload: Mapping[str, Any], day: date) -> bool:
    """Whether a change, as a payload, reaches `day`: in its range and not on
    a weekday it leaves out."""
    first, last = days_of(payload)
    return first <= day <= last and day.isoweekday() not in payload["excluded_weekdays"]


def same_class_time(one: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Whether two changes, as payloads, reach a class time in common: a day
    both cover and a period both name, no periods being the whole day."""
    if (
        one["periods"] is not None
        and other["periods"] is not None
        and not set(one["periods"]) & set(other["periods"])
    ):
        return False
    first = max(days_of(one)[0], days_of(other)[0])
    last = min(days_of(one)[1], days_of(other)[1])
    left_out = set(one["excluded_weekdays"]) | set(other["excluded_weekdays"])
    # A week of the days both span holds every weekday they could share.
    return any(
        (first + timedelta(days=n)).isoweekday() not in left_out
        for n in range(min((last - first).days + 1, 7))
    )


async def lock_courses(
    session: AsyncSession, term: str, course_nos: Iterable[str]
) -> TermDates | None:
    """Take the lock every writer of `course_changes` takes for each course,
    in order, then the term's row FOR KEY SHARE. Returns the term's first
    and last day of classes, None for a term not published."""
    for course_no in sorted(set(course_nos)):
        await session.execute(_LOCK, {"key": f"course_changes|{term}|{course_no}"})
    row = (
        await session.execute(
            select(SemesterTerm.start_date, SemesterTerm.end_date)
            .where(SemesterTerm.code == term)
            .with_for_update(read=True, key_share=True)
        )
    ).first()
    return None if row is None else (row.start_date, row.end_date)


async def has_schedule(session: AsyncSession, term: str, course_no: str) -> bool:
    """Whether a student takes the course in the term with class times."""
    return not await session.scalar(_NO_MEETINGS, {"term": term, "course": course_no})


async def is_class_day(session: AsyncSession, term: str, course_no: str, day: date) -> bool:
    """Whether the course meets on `day` in the term, a holiday not."""
    found = await session.execute(
        _VIOLATIONS,
        {
            "term": term, "course": course_no, "kind": "assessment", "mode": "day",
            "first": day, "last": day, "excluded": [], "periods": None, "editing": None,
        },
    )
    return not {row.code for row in found} & _NOT_CLASS_DAY


@dataclass(frozen=True)
class Announcement:
    """What the checks read of an announcement and its extraction."""

    text: str
    labels: Sequence[str]
    group_id: int
    posted_at: datetime
    visible_from: datetime | None
    llm_labels: Sequence[str]
    certain: bool


def passes(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(check["ok"] for check in checks.values())


async def check_change(
    session: AsyncSession,
    *,
    term: str,
    term_days: TermDates | None,
    course_no: str,
    payload: Mapping[str, Any],
    evidence: str,
    announcement: Announcement,
    editing: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Each check of `payload` for `course_no`, by name, each with `ok`."""
    first, last = days_of(payload)
    periods = payload["periods"]
    well_formed = _well_formed(payload, first, last)
    found = (
        await session.execute(
            _VIOLATIONS,
            {
                "term": term,
                "course": course_no,
                "kind": payload["kind"],
                "mode": payload["mode"],
                "first": first,
                "last": last,
                "excluded": payload["excluded_weekdays"],
                # The function takes NULL, every period, for a list the check refuses anyway.
                "periods": periods if well_formed else None,
                "editing": editing,
            },
        )
    ).all()
    codes = {row.code for row in found}
    broken = sorted(codes - _NOT_CLASS_DAY) + ([] if well_formed else ["format"])
    wanted = _LABELS.get(payload["kind"], _CLASS_CHANGE)
    return {
        "evidence": {"ok": bool(evidence.strip()) and evidence in announcement.text},
        "term": {
            "ok": term_days is not None and term_days[0] <= first and last <= term_days[1]
        },
        "class_day": {"ok": not codes & _NOT_CLASS_DAY},
        "weekday": {"ok": stated_weekdays(evidence) <= _weekdays(payload, first, last)},
        "rules": {
            "ok": not broken,
            "codes": broken,
            "change_ids": sorted({row.change_id for row in found if row.code == "clash"}),
        },
        "labels": {
            "ok": bool(wanted & set(announcement.labels))
            and bool(wanted & set(announcement.llm_labels))
        },
        "certain": {"ok": announcement.certain},
        "visibility": {
            "ok": announcement.group_id == -1
            and (
                announcement.visible_from is None
                or announcement.visible_from <= announcement.posted_at
            )
        },
    }


def stated_weekdays(evidence: str) -> set[int]:
    """The ISO weekdays `evidence` names: 週三, 星期三, （三）, Wed."""
    days = {_WEEKDAY_CHARS[a or b] for a, b in _ZH_WEEKDAY.findall(evidence)}
    return days | {_EN_WEEKDAYS[name.lower()] for name in _EN_WEEKDAY.findall(evidence)}


def _weekdays(payload: Mapping[str, Any], first: date, last: date) -> set[int]:
    span = min((last - first).days, 6)
    return {
        (first + timedelta(days=n)).isoweekday() for n in range(span + 1)
    } - set(payload["excluded_weekdays"])


def _well_formed(payload: Mapping[str, Any], first: date, last: date) -> bool:
    """Whether `course_changes` would take the payload as it stands."""
    if payload["mode"] == "day":
        days = first == last and not payload["excluded_weekdays"]
    else:
        days = first <= last
    periods = payload["periods"]
    kind = payload["kind"]
    values = all(
        isinstance(payload[field], str) and payload[field].strip()
        for field in _KIND_FIELDS.get(kind, ())
        if kind not in _OPTIONAL_FIELDS or payload[field] is not None
    )
    room = payload["room"]
    return (
        days
        and values
        and (periods is None or (bool(periods) and set(periods) <= set(PERIOD_RANK)))
        and (room is None or len(room.strip()) <= ROOM_MAX)
    )


@dataclass(frozen=True)
class Statement:
    """What an announcement's current extraction, the latest of its stored
    version, says of its courses."""

    announcement_id: str
    posted_at: datetime
    course_codes: frozenset[str]
    changes: tuple[dict[str, Any], ...]
    held: tuple[date, ...]


_STATEMENTS = text(
    """
SELECT a.id, a.posted_at, a.course_codes, e.raw_output
FROM course_announcements AS a
JOIN LATERAL (
    SELECT raw_output FROM course_announcement_extractions AS x
    WHERE x.announcement_id = a.id AND x.status = 'done' AND x.version = a.modified_at
    ORDER BY x.id DESC LIMIT 1
) AS e ON true
WHERE a.term_code = :term AND a.course_codes && :codes
"""
).bindparams(bindparam("codes", type_=ARRAY(Text)))


async def course_statements(
    session: AsyncSession, term: str, course_nos: Iterable[str]
) -> list[Statement]:
    """The statements of every announcement of the term that names one of
    `course_nos`."""
    rows = await session.execute(_STATEMENTS, {"term": term, "codes": sorted(set(course_nos))})
    return [
        Statement(
            announcement_id=row.id,
            posted_at=row.posted_at,
            course_codes=frozenset(row.course_codes),
            changes=tuple(change_payload(fact) for fact in row.raw_output["course_changes"]),
            held=tuple(date.fromisoformat(h["date"]) for h in row.raw_output["held_as_usual"]),
        )
        for row in rows
    ]


def contradiction(
    *,
    course_no: str,
    payload: Mapping[str, Any],
    announcement_id: str,
    posted_at: datetime,
    statements: Iterable[Statement],
) -> dict[str, Any]:
    """The contradiction check: whether an announcement of the course posted
    later holds a day of the change as usual, or states another change of
    that kind for it, naming the latest that does."""
    by: Statement | None = None
    for statement in statements:
        if (
            statement.announcement_id == announcement_id
            or course_no not in statement.course_codes
            or statement.posted_at <= posted_at
            or (by is not None and statement.posted_at <= by.posted_at)
        ):
            continue
        if any(covers_day(payload, day) for day in statement.held) or any(
            other["kind"] == payload["kind"]
            and same_class_time(other, payload)
            and any(other[field] != payload[field] for field in VALUE_FIELDS)
            for other in statement.changes
        ):
            by = statement
    return {"ok": by is None, "announcement_id": by and by.announcement_id}
