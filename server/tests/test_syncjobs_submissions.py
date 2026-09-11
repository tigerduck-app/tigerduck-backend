"""Which assignments get probed, and what a probe result does to the row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.auth.models import User
from server.syncjobs.moodle_client import FetchedSubmission
from server.syncjobs.submissions import (
    apply_submission_status,
    select_assignment_ids,
)

# Every other db_session-based test module in this suite pins the test's
# own event loop to the session-scoped one via this module-level mark
# (`prepared_engine`/`db_session` are `loop_scope="session"` fixtures; a
# bare `@pytest.mark.asyncio` per test leaves the test on the default
# function-scoped loop, which cannot use a session-scoped asyncpg
# connection). Matching that convention here instead of decorating each
# test individually.
pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)


async def test_selects_only_unsubmitted_inside_the_window(db_session, make_assignment):
    inside = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    await make_assignment(
        moodle_assignment_id=2, due_at=NOW + timedelta(hours=100)
    )
    await make_assignment(
        moodle_assignment_id=3, due_at=NOW - timedelta(hours=1)
    )
    await make_assignment(
        moodle_assignment_id=4,
        due_at=NOW + timedelta(hours=10),
        provider_is_submitted=True,
    )
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=inside.user_id, now=NOW, window_hours=48
    )

    assert ids == [1]


async def test_assignments_without_a_due_date_are_not_probed(
    db_session, make_assignment
):
    row = await make_assignment(moodle_assignment_id=5, due_at=None)
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=row.user_id, now=NOW, window_hours=48
    )

    assert ids == []


async def test_apply_marks_submitted(db_session, make_assignment):
    row = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    await db_session.flush()
    submitted_at = NOW - timedelta(hours=2)

    changed = await apply_submission_status(
        db_session,
        user_id=row.user_id,
        fetched={
            1: FetchedSubmission(
                moodle_assignment_id=1,
                is_submitted=True,
                submitted_at=submitted_at,
            )
        },
        now=NOW,
    )

    await db_session.refresh(row)
    assert changed == {1}
    assert row.provider_is_submitted is True
    assert row.provider_submitted_at == submitted_at


async def test_apply_leaves_unsubmitted_rows_untouched(db_session, make_assignment):
    row = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    await db_session.flush()

    changed = await apply_submission_status(
        db_session,
        user_id=row.user_id,
        fetched={
            1: FetchedSubmission(
                moodle_assignment_id=1, is_submitted=False, submitted_at=None
            )
        },
        now=NOW,
    )

    await db_session.refresh(row)
    assert changed == set()
    assert row.provider_is_submitted is False


async def test_apply_ignores_assignments_absent_from_the_probe(
    db_session, make_assignment
):
    row = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    await db_session.flush()

    changed = await apply_submission_status(
        db_session, user_id=row.user_id, fetched={}, now=NOW
    )

    await db_session.refresh(row)
    assert changed == set()
    assert row.provider_is_submitted is False


async def test_apply_reports_each_flip_once(db_session, make_assignment):
    # The caller cancels reminders for every id returned. Reporting a row
    # that was already submitted would redo that work on every sync.
    row = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    await db_session.flush()
    fetched = {
        1: FetchedSubmission(
            moodle_assignment_id=1, is_submitted=True, submitted_at=NOW
        )
    }

    first = await apply_submission_status(
        db_session, user_id=row.user_id, fetched=fetched, now=NOW
    )
    await db_session.flush()
    second = await apply_submission_status(
        db_session, user_id=row.user_id, fetched=fetched, now=NOW
    )

    assert first == {1}
    assert second == set()


# --- Supplementary boundary/delta coverage -----------------------------
#
# The tests above are the brief's, verbatim. The ones below close gaps the
# task's testing-discipline mandate calls out by name: `deleted_at` is part
# of the selection rule but no brief test constructs a soft-deleted row,
# and the window's upper edge is only exercised 52 hours away from the
# boundary (100h due date vs. a 48h window) -- comfortably not "the
# boundary itself". Each test below differs from a row that WOULD be
# selected in exactly one attribute.


async def test_soft_deleted_assignment_is_not_selected(db_session, make_assignment):
    row = await make_assignment(
        moodle_assignment_id=1,
        due_at=NOW + timedelta(hours=10),
        deleted_at=NOW - timedelta(hours=1),
    )
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=row.user_id, now=NOW, window_hours=48
    )

    assert ids == []


async def test_due_exactly_at_window_edge_is_selected(db_session, make_assignment):
    row = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=48)
    )
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=row.user_id, now=NOW, window_hours=48
    )

    assert ids == [1]


async def test_due_just_past_the_window_edge_is_excluded(db_session, make_assignment):
    row = await make_assignment(
        moodle_assignment_id=1,
        due_at=NOW + timedelta(hours=48) + timedelta(microseconds=1),
    )
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=row.user_id, now=NOW, window_hours=48
    )

    assert ids == []


async def test_due_exactly_now_is_excluded(db_session, make_assignment):
    row = await make_assignment(moodle_assignment_id=1, due_at=NOW)
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=row.user_id, now=NOW, window_hours=48
    )

    assert ids == []


async def test_assignment_belonging_to_a_different_user_is_not_selected(
    db_session, make_assignment
):
    mine = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    other_user = User()
    db_session.add(other_user)
    await db_session.flush()
    await make_assignment(
        moodle_assignment_id=2,
        due_at=NOW + timedelta(hours=10),
        user_id=other_user.id,
    )
    await db_session.flush()

    ids = await select_assignment_ids(
        db_session, user_id=mine.user_id, now=NOW, window_hours=48
    )

    assert ids == [1]


async def test_apply_returns_only_the_newly_flipped_ids_not_every_submitted_row(
    db_session, make_assignment
):
    # A function that returned "every row the probe marked submitted"
    # would answer {1, 2} here. The delta is {2} -- row 1 was already
    # submitted before this call.
    already = await make_assignment(
        moodle_assignment_id=1,
        due_at=NOW + timedelta(hours=10),
        provider_is_submitted=True,
    )
    await make_assignment(moodle_assignment_id=2, due_at=NOW + timedelta(hours=10))
    await db_session.flush()

    changed = await apply_submission_status(
        db_session,
        user_id=already.user_id,
        fetched={
            1: FetchedSubmission(
                moodle_assignment_id=1, is_submitted=True, submitted_at=NOW
            ),
            2: FetchedSubmission(
                moodle_assignment_id=2, is_submitted=True, submitted_at=NOW
            ),
        },
        now=NOW,
    )

    assert changed == {2}


async def test_apply_does_not_touch_another_users_matching_assignment_id(
    db_session, make_assignment
):
    mine = await make_assignment(
        moodle_assignment_id=1, due_at=NOW + timedelta(hours=10)
    )
    other_user = User()
    db_session.add(other_user)
    await db_session.flush()
    theirs = await make_assignment(
        moodle_assignment_id=1,
        due_at=NOW + timedelta(hours=10),
        user_id=other_user.id,
    )
    await db_session.flush()

    changed = await apply_submission_status(
        db_session,
        user_id=mine.user_id,
        fetched={
            1: FetchedSubmission(
                moodle_assignment_id=1, is_submitted=True, submitted_at=NOW
            )
        },
        now=NOW,
    )

    await db_session.refresh(mine)
    await db_session.refresh(theirs)
    assert changed == {1}
    assert mine.provider_is_submitted is True
    assert theirs.provider_is_submitted is False
