"""Mirror fetched Moodle enrolled courses into user_courses + changelog.

A successful fetch is the authoritative snapshot of the student's Moodle
enrollments: new rows insert, changed fields update, rows absent from the
fetch soft-delete, previously-deleted rows that reappear resurrect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.changelog import append_change, lock_sync_state
from server.sync.models import ChangeEntityType, UserCourse
from server.syncjobs.moodle_client import FetchedCourse

logger = structlog.get_logger(__name__)

_PROVIDER_FIELDS = ("course_name", "moodle_id")


@dataclass(frozen=True)
class CourseSyncStats:
    fetched_count: int
    changed_count: int


async def apply_fetched_courses(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    fetched: list[FetchedCourse],
    now: datetime,
) -> CourseSyncStats:
    locked_state = await lock_sync_state(session, user_id)

    async def log(entity_id: str, operation: str, hint: dict | None = None):
        await append_change(
            session,
            user_id=user_id,
            entity_type=ChangeEntityType.course.value,
            entity_id=entity_id,
            operation=operation,
            payload=hint,
            device_id=None,
            locked_state=locked_state,
        )

    existing_rows = (
        (
            await session.execute(
                select(UserCourse).where(UserCourse.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    existing = {r.moodle_id: r for r in existing_rows if r.moodle_id}

    changed = 0
    seen: set[str] = set()

    for item in fetched:
        moodle_id = str(item.moodle_course_id)
        seen.add(moodle_id)
        row = existing.get(moodle_id)

        if row is None:
            course_no = (item.short_name or moodle_id)[:64]
            row = UserCourse(
                user_id=user_id,
                semester="",
                course_key=f"moodle:{moodle_id}",
                source="ntust_portal",
                course_no=course_no,
                course_name=item.full_name,
                moodle_id=moodle_id,
                fetched_at=now,
                last_seen_at=now,
            )
            session.add(row)
            await session.flush()
            existing[moodle_id] = row
            changed += 1
            await log(str(row.id), "upsert", {"fields": ["course_name", "moodle_id"]})
            continue

        fields: list[str] = []
        if row.course_name != item.full_name:
            row.course_name = item.full_name
            fields.append("course_name")
        if row.deleted_at is not None:
            row.deleted_at = None
            fields.append("deleted_at")
        row.fetched_at = now
        row.last_seen_at = now
        if fields:
            changed += 1
            await log(str(row.id), "upsert", {"fields": fields})

    for moodle_id, row in existing.items():
        if moodle_id in seen or row.deleted_at is not None:
            continue
        row.deleted_at = now
        changed += 1
        await log(str(row.id), "delete")

    logger.info(
        "syncjobs.courses.applied",
        user_id=str(user_id),
        fetched=len(fetched),
        changed=changed,
    )
    return CourseSyncStats(fetched_count=len(fetched), changed_count=changed)
