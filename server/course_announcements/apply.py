"""Automatic apply: with `course_announcements_auto_apply` on, the add items
of an extraction are written to `course_changes`, all or none.

They apply only when every one passes every check, the extraction asks to
replace or remove nothing, and no earlier item of the announcement for the
same course, day and kind was rejected or removed by an operator. Otherwise,
and always with the setting off, they wait for review in the portal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.models import CourseChange
from server.admin_audit import AdminAuditLog
from server.course_announcements.checks import Announcement, check_change, days_of, passes
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
    ItemAction,
    ItemState,
)

logger = structlog.get_logger(__name__)

# `created_by`, `updated_by`, `decided_by` and the audit actor of what this writes.
ACTOR = "course-announcements"


class _Held(Exception):
    """An add item no longer passes once the others before it are written."""


async def apply_items(
    session: AsyncSession,
    announcement: CourseAnnouncement,
    context: Announcement,
    items: Sequence[CourseAnnouncementItem],
    *,
    term_days: tuple[date, date] | None,
    now: datetime,
) -> int:
    """Write the add items among `items`, one extraction's, if they may
    apply. The caller holds the lock of every course code they name.
    Returns how many were written."""
    adds = [
        item
        for item in items
        if item.action == ItemAction.add.value and item.state == ItemState.pending.value
    ]
    if (
        not adds
        or not all(item.passes_checks for item in adds)
        or any(
            item.action in (ItemAction.replace.value, ItemAction.remove.value) for item in items
        )
        or await _decided_against(session, announcement.id, adds)
    ):
        return 0
    try:
        async with session.begin_nested():
            for item in adds:
                await _write(session, announcement, context, item, term_days=term_days, now=now)
    except _Held:
        return 0
    logger.info("course_announcements.applied", announcement_id=announcement.id, changes=len(adds))
    return len(adds)


async def _write(
    session: AsyncSession,
    announcement: CourseAnnouncement,
    context: Announcement,
    item: CourseAnnouncementItem,
    *,
    term_days: tuple[date, date] | None,
    now: datetime,
) -> None:
    # Again, since an earlier item of the same extraction may now clash with it.
    checks = await check_change(
        session,
        term=announcement.term_code,
        term_days=term_days,
        course_no=item.course_no,
        payload=item.payload,
        evidence=item.evidence or "",
        announcement=context,
    )
    if not passes(checks):
        raise _Held
    first, last = days_of(item.payload)
    fields = {
        key: value for key, value in item.payload.items() if key not in ("start_date", "end_date")
    }
    change = CourseChange(
        **fields,
        term_code=announcement.term_code,
        course_no=item.course_no,
        start_date=first,
        end_date=last,
        created_by=ACTOR,
        updated_by=ACTOR,
        source_announcement_id=announcement.id,
    )
    session.add(change)
    await session.flush()
    session.add(
        AdminAuditLog(
            actor=ACTOR,
            action="course_change.create",
            detail={
                "change_id": change.id,
                "term": announcement.term_code,
                "course_no": item.course_no,
                "kind": change.kind,
                "announcement_id": announcement.id,
            },
        )
    )
    item.state = ItemState.applied.value
    item.course_change_id = change.id
    item.decided_by = ACTOR
    item.decided_at = now


async def _decided_against(
    session: AsyncSession, announcement_id: str, adds: Sequence[CourseAnnouncementItem]
) -> bool:
    """Whether an operator rejected or removed an earlier item of the
    announcement for the course, day and kind of one of `adds`."""
    decided = await session.execute(
        select(CourseAnnouncementItem.course_no, CourseAnnouncementItem.payload)
        .join(
            CourseAnnouncementExtraction,
            CourseAnnouncementExtraction.id == CourseAnnouncementItem.extraction_id,
        )
        .where(
            CourseAnnouncementExtraction.announcement_id == announcement_id,
            CourseAnnouncementItem.state.in_(
                [ItemState.rejected.value, ItemState.removed.value]
            ),
        )
    )
    refused = {
        (course_no, payload.get("kind"), payload.get("start_date"))
        for course_no, payload in decided.all()
    }
    return any(
        (item.course_no, item.payload["kind"], item.payload["start_date"]) in refused
        for item in adds
    )
