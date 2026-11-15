"""The one-time import of course announcement history from a local export.

The export holds one Moodle course per JSON line: `course` with its `id`,
`idnumber` and `fullname`, and `discussions`, its news forum's first posts as
`mod_forum_get_forum_discussions` returns them, keeping only the fields
`parse_discussion` reads. The import refuses any other field, an author's
among them, before it stores anything, then stores and classifies through
the poll's own path, so an imported version and a polled one meet the same
rules and only the current term's reach the LLM.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.terms import current_term_code
from server.course_announcements.classifier import Classifier
from server.course_announcements.ingest import (
    classify_pending,
    queue_extractions,
    store_announcements,
)
from server.db import BIGINT_VALUES, session_scope
from server.syncjobs.assignments import course_nos, moodle_semester
from server.syncjobs.moodle_client import FetchedCourse, FetchedDiscussion, parse_discussion

RECORD_FIELDS = frozenset({"course", "discussions"})
COURSE_FIELDS = frozenset({"id", "idnumber", "fullname"})
DISCUSSION_FIELDS = frozenset(
    {
        "id",
        "discussion",
        "subject",
        "message",
        "created",
        "modified",
        "groupid",
        "timestart",
        "attachment",
    }
)


class ExportRefused(Exception):
    """The export cannot be imported. The message names a line and a field,
    never a value."""


@dataclass(frozen=True)
class ExportedCourse:
    moodle_course_id: int
    # None for a course whose idnumber names no term.
    term_code: str | None
    course_codes: list[str]
    discussions: list[FetchedDiscussion]


@dataclass(frozen=True)
class ImportResult:
    stored: int
    classified: int
    # Courses whose idnumber names no term, so none of their posts were stored.
    skipped_courses: int


def _fields(number: int, value: object, allowed: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        raise ExportRefused(f"line {number}: expected an object")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ExportRefused(f"line {number}: field {unexpected[0]!r} is not in the export format")
    return value


def _course(number: int, line: str) -> ExportedCourse:
    try:
        parsed = json.loads(line)
    except ValueError:
        raise ExportRefused(f"line {number}: not JSON") from None
    record = _fields(number, parsed, RECORD_FIELDS)
    course = _fields(number, record.get("course"), COURSE_FIELDS)
    items = record.get("discussions")
    if not isinstance(items, list):
        raise ExportRefused(f"line {number}: no discussion list")
    discussions = [parse_discussion(_fields(number, item, DISCUSSION_FIELDS)) for item in items]
    course_id, idnumber = course.get("id"), course.get("idnumber")
    full_name = course.get("fullname")
    if (
        type(course_id) is not int
        or course_id not in BIGINT_VALUES
        or not isinstance(idnumber, str)
        or not isinstance(full_name, str)
        or None in discussions
    ):
        raise ExportRefused(f"line {number}: a value is missing or malformed")
    fetched = FetchedCourse(
        moodle_course_id=course_id,
        short_name=None,
        full_name=full_name,
        category_id=None,
        enrolled_user_count=None,
        idnumber=idnumber,
    )
    return ExportedCourse(
        moodle_course_id=course_id,
        term_code=moodle_semester(idnumber),
        course_codes=course_nos(fetched),
        discussions=discussions,
    )


def read_export(lines: Iterable[str]) -> list[ExportedCourse]:
    """Every course of the export, or ExportRefused for the first line that
    is not as the export writes it."""
    return [_course(number, line) for number, line in enumerate(lines, 1) if line.strip()]


async def import_history(
    session_factory: async_sessionmaker[AsyncSession],
    classifier: Classifier,
    courses: Sequence[ExportedCourse],
) -> ImportResult:
    """Store every course's posts unless a version as late is stored, then
    classify until nothing waits, which queues the current term's for
    extraction as a poll's are. What the service leaves unclassified, the
    poll tick classifies later."""
    stored = skipped = 0
    for course in courses:
        if course.term_code is None:
            skipped += 1
            continue
        async with session_scope(session_factory) as session:
            stored += len(
                await store_announcements(
                    session,
                    term_code=course.term_code,
                    moodle_course_id=course.moodle_course_id,
                    course_codes=course.course_codes,
                    discussions=course.discussions,
                )
            )
    classified = 0
    while batch := await classify_pending(session_factory, classifier):
        classified += batch
    return ImportResult(stored=stored, classified=classified, skipped_courses=skipped)


async def queue_reextraction(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> int:
    """Queue every announcement of the current term the route rule sends for
    extraction again, as a model change needs. Returns how many were queued."""
    async with session_scope(session_factory) as session:
        return await queue_extractions(session, await current_term_code(session, now))
