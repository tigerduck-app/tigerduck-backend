"""Narrow-window submission-status refresh.

`apply_fetched_assignments` deliberately never writes submission state — the
assignment list endpoint does not carry it. This module fills that gap for
the only assignments whose answer matters soon: the ones about to enter a
reminder window.

Kept separate from `assignments.py` because it is a different Moodle call
with a different failure posture: a missing answer here means "leave the row
as it was", never "the row is unsubmitted".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.models import UserAssignment
from server.syncjobs.moodle_client import FetchedSubmission

logger = structlog.get_logger(__name__)


async def select_assignment_ids(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    now: datetime,
    window_hours: int,
) -> list[int]:
    """Moodle assignment ids worth probing.

    Excludes soft-deleted rows, rows already marked submitted (Moodle never
    un-submits), rows already past due (their reminder window has closed),
    and rows with no due date at all (nothing to remind about).
    """
    horizon = now + timedelta(hours=window_hours)
    rows = (
        await session.execute(
            select(UserAssignment.moodle_assignment_id).where(
                UserAssignment.user_id == user_id,
                UserAssignment.deleted_at.is_(None),
                UserAssignment.provider_is_submitted.is_(False),
                UserAssignment.due_at.is_not(None),
                UserAssignment.due_at > now,
                UserAssignment.due_at <= horizon,
            )
        )
    ).scalars().all()
    return sorted(rows)


async def apply_submission_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    fetched: dict[int, FetchedSubmission],
    now: datetime,
) -> set[int]:
    """Write probe results back. Returns the moodle assignment ids that
    flipped from unsubmitted to submitted in this call.

    A set rather than a count, because the caller cancels reminders and
    ends Live Activities for exactly these assignments.

    Only submissions flip a row. A `is_submitted=False` result is not
    written: the row already says that, and a probe that came back
    negative because of a transient Moodle state must not overwrite a
    submission the client reported.
    """
    if not fetched:
        return set()

    submitted_ids = [
        aid for aid, item in fetched.items() if item.is_submitted
    ]
    if not submitted_ids:
        return set()

    rows = (
        await session.execute(
            select(UserAssignment).where(
                UserAssignment.user_id == user_id,
                UserAssignment.deleted_at.is_(None),
                UserAssignment.moodle_assignment_id.in_(submitted_ids),
                UserAssignment.provider_is_submitted.is_(False),
            )
        )
    ).scalars().all()

    changed: set[int] = set()
    for row in rows:
        item = fetched[row.moodle_assignment_id]
        row.provider_is_submitted = True
        row.provider_submitted_at = item.submitted_at or now
        changed.add(row.moodle_assignment_id)

    if changed:
        # `Session.refresh()` expires an instance's attributes *before* it
        # autoflushes -- so a caller that refreshes a row we just mutated,
        # without an intervening flush, would silently lose the write:
        # expire discards the pending in-memory value before autoflush ever
        # sees it as dirty. Flushing here, synchronously with the mutation,
        # makes the write visible to any read in this transaction
        # regardless of what the caller does next.
        await session.flush()
        logger.info("syncjobs.submissions.applied", changed=len(changed))
    return changed
