"""apply_fetched_assignments: insert / update / soft-delete / resurrect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import User
from server.sync.models import UserAssignment, UserChangeLog, UserCourse
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.moodle_client import FetchedAssignment

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime.now(UTC)


def _fa(assignment_id: int, **kwargs) -> FetchedAssignment:
    defaults = dict(
        moodle_course_id=7001,
        moodle_assignment_id=assignment_id,
        course_name="資料結構",
        title=f"HW{assignment_id}",
        due_at=NOW + timedelta(days=7),
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=(
            f"https://moodle.example.edu/mod/assign/view.php?id={assignment_id}"
        ),
        intro_html=None,
    )
    defaults.update(kwargs)
    return FetchedAssignment(**defaults)


async def _make_user(session) -> User:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    return user


async def _changelog(session, user_id):
    return (
        (
            await session.execute(
                select(UserChangeLog)
                .where(UserChangeLog.user_id == user_id)
                .order_by(UserChangeLog.revision)
            )
        )
        .scalars()
        .all()
    )


async def test_inserts_new_assignments_with_changelog(db_session):
    user = await _make_user(db_session)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    assert stats.fetched_count == 2
    assert stats.changed_count == 2
    rows = (
        (
            await db_session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.moodle_assignment_id for r in rows} == {1, 2}
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert all(e.entity_type == "assignment" for e in entries)
    assert all(e.operation == "upsert" for e in entries)
    assert all(e.device_id is None for e in entries)


async def test_unchanged_assignment_writes_no_changelog(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    later = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=later
    )
    await db_session.commit()

    assert stats.changed_count == 0
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 1
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.last_seen_at == later  # metadata still refreshed
    assert row.fetched_at == later


async def test_changed_field_updates_and_logs_field_names(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    new_due = NOW + timedelta(days=14)
    stats = await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1, due_at=new_due, title="HW1-renamed")],
        now=NOW + timedelta(hours=8),
    )
    await db_session.commit()

    assert stats.changed_count == 1
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert set(entries[-1].payload["fields"]) == {"due_at", "title"}
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.title == "HW1-renamed"
    assert row.due_at == new_due


async def test_absent_assignment_soft_deleted_then_resurrected(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    t2 = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=t2
    )
    await db_session.commit()
    assert stats.changed_count == 1
    gone = (
        await db_session.execute(
            select(UserAssignment).where(
                UserAssignment.user_id == user.id,
                UserAssignment.moodle_assignment_id == 2,
            )
        )
    ).scalar_one()
    assert gone.deleted_at is not None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "delete"
    assert entries[-1].entity_id == str(gone.id)

    t3 = NOW + timedelta(hours=16)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=t3
    )
    await db_session.commit()
    await db_session.refresh(gone)
    assert gone.deleted_at is None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "upsert"


async def test_links_assignment_to_course_by_moodle_id(db_session):
    user = await _make_user(db_session)
    course = UserCourse(
        user_id=user.id,
        semester="1142",
        course_key="CS2006301",
        course_no="CS2006301",
        course_name="資料結構",
        moodle_id="7001",
    )
    db_session.add(course)
    await db_session.flush()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.user_course_id == course.id
    assert row.course_no == "CS2006301"


async def test_never_touches_provider_submission_fields(db_session):
    user = await _make_user(db_session)
    existing = UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=1,
        title="HW1",
        provider_is_submitted=True,
        provider_submitted_at=NOW - timedelta(days=1),
    )
    db_session.add(existing)
    await db_session.commit()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, title="HW1")], now=NOW
    )
    await db_session.commit()
    await db_session.refresh(existing)
    assert existing.provider_is_submitted is True
    assert existing.provider_submitted_at is not None
