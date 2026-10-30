"""Mirror fetched Moodle assignments into user_assignments + changelog.

A successful fetch is treated as the authoritative snapshot of the
account's assignments: new rows insert, changed provider fields update,
rows absent from the fetch soft-delete, previously-deleted rows that
reappear resurrect, and an app's placeholder (course id 0) for an
assignment the fetch lists becomes its row, one row per assignment. Each
row is linked to the class table as the iPhone app files it, which is what
the reminder scan selects by.
Submission state is not written *here* — the
assignment list endpoint does not carry it. `submissions.py` refreshes
it separately for assignments about to enter a reminder window, and
client-uploaded values survive everywhere else.

Changelog appends run under the per-user sync-state lock
(`lock_sync_state`), acquired once per run; payloads carry routing hints
(changed field names) only.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.terms import current_term_code
from server.sync.changelog import append_change, lock_sync_state
from server.sync.models import ChangeEntityType, UserAssignment, UserCourse
from server.syncjobs.moodle_client import FetchedAssignment, FetchedCourse

logger = structlog.get_logger(__name__)

# Provider-owned fields mirrored 1:1 from the fetch result.
_PROVIDER_FIELDS = (
    "course_name",
    "title",
    "due_at",
    "cutoff_at",
    "allow_from_at",
    "moodle_url",
    "intro_html",
)


def with_extension(
    item: FetchedAssignment, extension: datetime | None
) -> FetchedAssignment:
    """`item` with the dates that apply to a student with `extension`: an
    extension later than the due date or the cutoff moves it there. An
    assignment without a cutoff has none to move."""
    if extension is None:
        return item
    return replace(
        item,
        due_at=max(item.due_at, extension) if item.due_at else extension,
        cutoff_at=max(item.cutoff_at, extension) if item.cutoff_at else None,
    )


def kept_extension(
    item: FetchedAssignment, extension: datetime | None
) -> datetime | None:
    """`extension` while it is later than the due date Moodle listed, else
    None, so a stored extension always means the due date comes from it."""
    if extension is None or (item.due_at is not None and extension <= item.due_at):
        return None
    return extension


# A course number in a co-listed course's full name, as the iPhone app's
# `MoodleEnrolledCourse.courseNos` finds it.
_COURSE_NO = re.compile(r"(?:3?[A-Z]{2,3}|3[A-Z])[0-9][A-Z0-9]{5,6}")
_WORD = re.compile(r"[^\W_]+")
_NEVER = datetime.min.replace(tzinfo=UTC)


def moodle_semester(idnumber: str) -> str | None:
    """The term a Moodle idnumber starts with, upper-cased as NTUST spells a
    summer term ("114h" is "114H"), or None; `SDCourse.semesterPrefix`."""
    prefix = idnumber[:4]
    if len(idnumber) <= 4 or not prefix[:3].isnumeric():
        return None
    return prefix.upper() if prefix[3].isnumeric() or prefix[3].isalpha() else None


def _course_nos(course: FetchedCourse) -> list[str]:
    """The idnumber's course number, then the others in the full name."""
    if moodle_semester(course.idnumber) is None:
        return []
    nos = [course.idnumber[4:]]
    for word in _WORD.findall(course.full_name):
        if _COURSE_NO.fullmatch(word) and word not in nos:
            nos.append(word)
    return nos


def listed_courses(
    enrolled: Sequence[FetchedCourse], class_nos: set[str], term: str
) -> dict[int, str]:
    """The Moodle courses whose assignments the iPhone app lists in `term`,
    each with the course number it files them under, by Moodle course id.

    The app's assignment round, `runAssignmentRound` in tigerduck-app's
    swift/TigerDuck/Bridge/AppServiceBridge+Assignments.swift: every course
    of the term and every class-table course, one Moodle course per number.
    """
    nos = {course.moodle_course_id: _course_nos(course) for course in enrolled}

    def primary(course: FetchedCourse) -> str:
        return (nos[course.moodle_course_id] or [""])[0]

    wanted = class_nos | {
        primary(c) for c in enrolled if moodle_semester(c.idnumber) == term
    }
    candidates = [c for c in enrolled if primary(c) and primary(c) in wanted]
    siblings: dict[str, list[FetchedCourse]] = {}
    for course in candidates:
        siblings.setdefault(primary(course), []).append(course)
    listed: dict[int, str] = {}
    for course in candidates:
        same = siblings[primary(course)]
        this_term = [c for c in same if moodle_semester(c.idnumber) == term]
        # A retaken course: this term's Moodle course, else the latest begun.
        latest = max(same, key=lambda c: (c.start_at or _NEVER, c.moodle_course_id))
        if course in (this_term or [latest]):
            listed[course.moodle_course_id] = next(
                (no for no in nos[course.moodle_course_id] if no in class_nos),
                primary(course),
            )
    return listed


@dataclass(frozen=True)
class AssignmentSyncStats:
    fetched_count: int
    changed_count: int
    # Assignments whose stored due date this sync moved back to Moodle's.
    recheck_ids: frozenset[int] = frozenset()


async def apply_fetched_assignments(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    fetched: list[FetchedAssignment],
    now: datetime,
    enrolled: Sequence[FetchedCourse] | None = None,
    refused_course_ids: frozenset[int] = frozenset(),
) -> AssignmentSyncStats:
    """Apply one Moodle answer, soft-deleting the stored rows it leaves out.

    `enrolled` is the student's Moodle courses, None when they could not be
    read. A row of a course the iPhone app lists in the current term
    (`listed_courses`) is linked to the class table's course of that number,
    if there is one, and any other row is not. Without `enrolled`, or with
    none beside assignments, the rows keep their links and a new row stays
    unclassified.

    `refused_course_ids` are the courses an answer that lost every course
    refused (`MoodleNoCourseAccess`). It says nothing about them, so their
    rows stay, and so do the placeholders, which name no course (course id
    0) and may belong to one of them.
    """
    locked_state = await lock_sync_state(session, user_id)

    async def log(entity_id: str, operation: str, hint: dict | None = None):
        await append_change(
            session,
            user_id=user_id,
            entity_type=ChangeEntityType.assignment.value,
            entity_id=entity_id,
            operation=operation,
            payload=hint,
            device_id=None,
            locked_state=locked_state,
        )

    existing_rows = (
        (
            await session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    existing = {
        (r.moodle_course_id, r.moodle_assignment_id): r for r in existing_rows
    }

    class_table: dict[str, UserCourse] = {}
    listed: dict[int, str] = {}
    if not enrolled:
        # No course beside assignments contradicts them, so it decides nothing.
        enrolled = None
    term = await current_term_code(session, now) if enrolled is not None else None
    if term is not None:
        class_table = {
            c.course_no: c
            for c in await session.scalars(
                select(UserCourse).where(
                    UserCourse.user_id == user_id, UserCourse.semester == term
                )
            )
            if c.course_no
        }
        listed = listed_courses(enrolled, set(class_table), term)
    # "" marks a row classified as of no term; None, one no fetch classified.
    semester_by_course = {
        c.moodle_course_id: moodle_semester(c.idnumber) or ""
        for c in enrolled or ()
    }

    changed = 0
    seen: set[tuple[int, int]] = set()
    recheck: set[int] = set()

    for item in fetched:
        key = (item.moodle_course_id, item.moodle_assignment_id)
        seen.add(key)
        course_no = listed.get(item.moodle_course_id)
        linked_course = class_table.get(course_no) if course_no else None
        row = existing.get(key)
        fields: list[str] = []
        placeholder_key = (0, item.moodle_assignment_id)
        if row is None and placeholder_key in existing:
            # An app uploaded it before any fetch listed it: that row, with
            # the marks and the submission on it, becomes the server's.
            row = existing.pop(placeholder_key)
            row.moodle_course_id = item.moodle_course_id
            existing[key] = row
            fields.append("moodle_course_id")
            # The app uploads the student's date; the probe right after checks it.
            if row.due_at and item.due_at and row.due_at > item.due_at:
                row.extension_due_at = row.due_at

        if row is None:
            row = UserAssignment(
                user_id=user_id,
                user_course_id=linked_course.id if linked_course else None,
                moodle_course_id=item.moodle_course_id,
                moodle_assignment_id=item.moodle_assignment_id,
                moodle_semester=(
                    semester_by_course.get(item.moodle_course_id, "")
                    if enrolled is not None
                    else None
                ),
                course_no=course_no,
                course_name=item.course_name,
                title=item.title,
                due_at=item.due_at,
                cutoff_at=item.cutoff_at,
                allow_from_at=item.allow_from_at,
                moodle_url=item.moodle_url,
                intro_html=item.intro_html,
                fetched_at=now,
                last_seen_at=now,
            )
            session.add(row)
            await session.flush()
            existing[key] = row
            changed += 1
            await log(str(row.id), "upsert", {"fields": list(_PROVIDER_FIELDS)})
            continue

        row.extension_due_at = kept_extension(item, row.extension_due_at)
        item = with_extension(item, row.extension_due_at)
        if row.due_at and item.due_at and row.due_at > item.due_at:
            # Maybe an extension an app uploaded; the probe after this sync decides.
            recheck.add(item.moodle_assignment_id)
        for field_name in _PROVIDER_FIELDS:
            new_value = getattr(item, field_name)
            if getattr(row, field_name) != new_value:
                setattr(row, field_name, new_value)
                fields.append(field_name)
        if enrolled is not None:
            linked_id = linked_course.id if linked_course else None
            if row.user_course_id != linked_id:
                row.user_course_id = linked_id
                fields.append("user_course_id")
            if course_no is not None and row.course_no != course_no:
                row.course_no = course_no
                fields.append("course_no")
            row.moodle_semester = semester_by_course.get(item.moodle_course_id, "")
        if row.deleted_at is not None:
            row.deleted_at = None
            fields.append("deleted_at")
        row.fetched_at = now
        row.last_seen_at = now
        if fields:
            changed += 1
            await log(str(row.id), "upsert", {"fields": fields})

    for key, row in existing.items():
        if key in seen or row.deleted_at is not None:
            continue
        if refused_course_ids and (
            row.moodle_course_id == 0 or row.moodle_course_id in refused_course_ids
        ):
            continue
        row.deleted_at = now
        changed += 1
        await log(str(row.id), "delete")

    logger.info(
        "syncjobs.assignments.applied",
        user_id=str(user_id),
        fetched=len(fetched),
        changed=changed,
    )
    return AssignmentSyncStats(
        fetched_count=len(fetched),
        changed_count=changed,
        recheck_ids=frozenset(recheck),
    )
