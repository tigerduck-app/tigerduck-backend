"""Mirror fetched Moodle assignments into user_assignments + changelog.

A successful fetch is treated as the authoritative snapshot of the
account's assignments: new rows insert, changed provider fields update,
rows absent from the fetch soft-delete, previously-deleted rows that
reappear resurrect. Submission state is not written *here* — the
assignment list endpoint does not carry it. `submissions.py` refreshes
it separately for assignments about to enter a reminder window, and
client-uploaded values survive everywhere else.

Changelog appends run under the per-user sync-state lock
(`lock_sync_state`), acquired once per run; payloads carry routing hints
(changed field names) only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.changelog import append_change, lock_sync_state
from server.sync.models import ChangeEntityType, UserAssignment, UserCourse
from server.syncjobs.moodle_client import FetchedAssignment

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


@dataclass(frozen=True)
class AssignmentSyncStats:
    fetched_count: int
    changed_count: int


async def apply_fetched_assignments(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    fetched: list[FetchedAssignment],
    now: datetime,
) -> AssignmentSyncStats:
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

    courses = (
        (
            await session.execute(
                select(UserCourse).where(
                    UserCourse.user_id == user_id,
                    UserCourse.moodle_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    course_by_moodle_id = {c.moodle_id: c for c in courses}

    changed = 0
    seen: set[tuple[int, int]] = set()

    for item in fetched:
        key = (item.moodle_course_id, item.moodle_assignment_id)
        seen.add(key)
        linked_course = course_by_moodle_id.get(str(item.moodle_course_id))
        row = existing.get(key)

        if row is None:
            row = UserAssignment(
                user_id=user_id,
                user_course_id=linked_course.id if linked_course else None,
                moodle_course_id=item.moodle_course_id,
                moodle_assignment_id=item.moodle_assignment_id,
                course_no=linked_course.course_no if linked_course else None,
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

        fields: list[str] = []
        for field_name in _PROVIDER_FIELDS:
            new_value = getattr(item, field_name)
            if getattr(row, field_name) != new_value:
                setattr(row, field_name, new_value)
                fields.append(field_name)
        if linked_course is not None and row.user_course_id != linked_course.id:
            row.user_course_id = linked_course.id
            row.course_no = linked_course.course_no
            fields.append("user_course_id")
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
        row.deleted_at = now
        changed += 1
        await log(str(row.id), "delete")

    logger.info(
        "syncjobs.assignments.applied",
        user_id=str(user_id),
        fetched=len(fetched),
        changed=changed,
    )
    return AssignmentSyncStats(fetched_count=len(fetched), changed_count=changed)
