"""Course membership by the TigerSync clock.

Per course key the user's last choice is an add (`user_courses.added_at`), a
hide or a removal (a tombstone's `deleted_at`), or none. A term reset is
`user_semester_resets.reset_at`, a full reset `users.courses_reset_at`. A
membership edit wins when its stamp is later than every one of these for its
key; a reset wins when later than the reset it would replace, and clears only
what is stamped at or before it. The school roster is a base layer: it never
brings back a hidden course, and a removal does not hide what it lists.

Callers hold the user's sync lock (`lock_sync_state`) from the reads here to
their writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import User
from server.sync.merge import from_clock, to_clock
from server.sync.models import (
    UserCourse,
    UserCourseOverride,
    UserCourseTombstone,
    UserSemesterReset,
)

HIDE = "hide"
REMOVE = "remove"
RESET = "reset"


def tombstone_kind(tombstone: UserCourseTombstone) -> str:
    if tombstone.deleted_by_reset:
        return RESET
    return REMOVE if tombstone.user_added else HIDE


def semester_of(course_key: str) -> str | None:
    """The term in a course key, which the apps build as `client:{term}:{no}`."""
    prefix, _, rest = course_key.partition(":")
    semester, _, course_no = rest.partition(":")
    return semester if prefix == "client" and semester and course_no else None


def latest(*stamps: datetime | None) -> datetime | None:
    present = [s for s in stamps if s is not None]
    return max(present) if present else None


class Resets:
    """The user's reset stamps: the full reset and one per term."""

    def __init__(self, full: datetime | None, terms: dict[str, datetime]) -> None:
        self.full = full
        self.terms = terms

    def covering(self, semester: str | None) -> datetime | None:
        return latest(self.full, self.terms.get(semester) if semester else None)

    @classmethod
    async def load(cls, session: AsyncSession, user_id: uuid.UUID) -> Resets:
        full = await session.scalar(select(User.courses_reset_at).where(User.id == user_id))
        rows = await session.execute(
            select(UserSemesterReset.semester, UserSemesterReset.reset_at).where(
                UserSemesterReset.user_id == user_id
            )
        )
        return cls(full, dict(rows.all()))


def membership_stamp(
    row: UserCourse | None,
    tombstone: UserCourseTombstone | None,
    resets: Resets,
    semester: str | None,
) -> datetime | None:
    """The stamp a membership edit of the key must beat."""
    return latest(
        row.added_at if row else None,
        tombstone.deleted_at if tombstone else None,
        resets.covering(semester),
    )


def override_outlasts(override: UserCourseOverride | None, reset_at: datetime) -> bool:
    """Whether the override holds a value stamped after the reset."""
    if override is None:
        return False
    stamps = (override.color_hex_updated_at, override.ignore_assignments_updated_at)
    if any(s is not None and s > reset_at for s in stamps):
        return True
    clock = to_clock(reset_at)
    return any(v > clock for v in (override.custom_names_updated_at or {}).values())


def _at_or_before(stamp: datetime | None, reset_at: datetime) -> bool:
    return stamp is None or stamp <= reset_at


def clear_before(row: UserCourse, override: UserCourseOverride | None, reset_at: datetime) -> bool:
    """Return each of the row's choices stamped at or before the reset to its
    empty, unstamped state; True when anything changed."""
    changed = False
    if row.added_at is not None and row.added_at <= reset_at:
        row.added_at = None
        changed = True
    if override is None:
        return changed
    stamp = override.color_hex_updated_at
    if (override.color_hex is not None or stamp) and _at_or_before(stamp, reset_at):
        override.color_hex = None
        override.color_hex_updated_at = None
        override.color_hex_device_id = None
        changed = True
    stamp = override.ignore_assignments_updated_at
    if (override.ignore_assignments or stamp) and _at_or_before(stamp, reset_at):
        override.ignore_assignments = False
        override.ignore_assignments_updated_at = None
        override.ignore_assignments_device_id = None
        changed = True
    clock = to_clock(reset_at)
    names = override.custom_names or {}
    stamps = override.custom_names_updated_at or {}
    kept = {k: v for k, v in stamps.items() if k in names and v > clock}
    if set(names) != set(kept):
        override.custom_names = {k: names[k] for k in kept}
        override.custom_names_updated_at = kept
        override.custom_name_updated_at = from_clock(max(kept.values())) if kept else None
        if not kept:
            override.custom_name_device_id = None
        changed = True
    return changed
