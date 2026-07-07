"""apply_fetched_courses: insert / update / delete scoping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.auth.models import User
from server.sync.models import UserChangeLog, UserCourse
from server.syncjobs.courses import apply_fetched_courses
from server.syncjobs.moodle_client import FetchedCourse

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime.now(UTC)


def _fc(course_id: int, **kwargs) -> FetchedCourse:
    defaults = dict(
        moodle_course_id=course_id,
        short_name=f"CS{course_id}",
        full_name=f"課程 {course_id}",
        category_id=None,
        enrolled_user_count=None,
    )
    defaults.update(kwargs)
    return FetchedCourse(**defaults)


async def _make_user(session) -> User:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    return user


def _client_course(
    user_id, semester: str, course_no: str, moodle_id: str | None = None
) -> UserCourse:
    """A row shaped like POST /sync/courses/upload creates: client: key,
    moodle_id backfilled from semester+course_no (or client-supplied)."""
    return UserCourse(
        user_id=user_id,
        semester=semester,
        course_key=f"client:{semester}:{course_no}",
        source="ntust_portal",
        course_no=course_no,
        course_name=f"客戶端課程 {course_no}",
        moodle_id=moodle_id or f"{semester}{course_no}",
        fetched_at=NOW,
        last_seen_at=NOW,
    )


async def _courses(session, user_id) -> list[UserCourse]:
    return (
        (
            await session.execute(
                select(UserCourse).where(UserCourse.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )


async def test_inserts_fetched_courses(db_session):
    user = await _make_user(db_session)
    stats = await apply_fetched_courses(
        db_session, user_id=user.id, fetched=[_fc(101), _fc(102)], now=NOW
    )
    await db_session.commit()

    assert stats.changed_count == 2
    rows = await _courses(db_session, user.id)
    assert {r.course_key for r in rows} == {"moodle:101", "moodle:102"}


async def test_deletes_moodle_course_absent_from_fetch(db_session):
    user = await _make_user(db_session)
    await apply_fetched_courses(
        db_session, user_id=user.id, fetched=[_fc(101), _fc(102)], now=NOW
    )
    await db_session.commit()

    stats = await apply_fetched_courses(
        db_session, user_id=user.id, fetched=[_fc(101)], now=NOW
    )
    await db_session.commit()

    assert stats.changed_count == 1
    rows = await _courses(db_session, user.id)
    assert {r.course_key for r in rows} == {"moodle:101"}
    delete_entries = (
        (
            await db_session.execute(
                select(UserChangeLog).where(
                    UserChangeLog.user_id == user.id,
                    UserChangeLog.operation == "delete",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(delete_entries) == 1


async def test_client_uploaded_courses_survive_fetch(db_session):
    """Client-uploaded rows always carry a moodle_id backfilled from
    semester+course_no; the Moodle mirror must never treat them as
    stale server rows and delete them."""
    user = await _make_user(db_session)
    db_session.add(_client_course(user.id, "1132", "CS0123"))
    db_session.add(_client_course(user.id, "1132", "EE0456"))
    await db_session.flush()

    stats = await apply_fetched_courses(
        db_session, user_id=user.id, fetched=[_fc(101)], now=NOW
    )
    await db_session.commit()

    rows = await _courses(db_session, user.id)
    assert {r.course_key for r in rows} == {
        "client:1132:CS0123",
        "client:1132:EE0456",
        "moodle:101",
    }
    # Only the insert of moodle:101 counts as a change.
    assert stats.changed_count == 1


async def test_empty_fetch_does_not_touch_client_courses(db_session):
    user = await _make_user(db_session)
    db_session.add(_client_course(user.id, "1132", "CS0123"))
    await db_session.flush()

    stats = await apply_fetched_courses(
        db_session, user_id=user.id, fetched=[], now=NOW
    )
    await db_session.commit()

    assert stats.changed_count == 0
    rows = await _courses(db_session, user.id)
    assert {r.course_key for r in rows} == {"client:1132:CS0123"}


async def test_fetch_does_not_hijack_client_row_sharing_moodle_id(db_session):
    """A client-uploaded course can carry a moodle_id equal to a real Moodle
    course id, so a client: row and a moodle: row legitimately share a moodle_id
    (documented in test_sync_overrides.py). The mirror must create its own
    moodle:<id> row for the enrollment and must never absorb / overwrite the
    client row."""
    user = await _make_user(db_session)
    db_session.add(_client_course(user.id, "1141", "CS3001301", moodle_id="777"))
    await db_session.flush()

    stats = await apply_fetched_courses(
        db_session,
        user_id=user.id,
        fetched=[_fc(777, full_name="OS (Moodle name)")],
        now=NOW,
    )
    await db_session.commit()

    rows = {r.course_key: r for r in await _courses(db_session, user.id)}
    # The mirror created its own row for the Moodle enrollment...
    assert "moodle:777" in rows
    assert rows["moodle:777"].course_name == "OS (Moodle name)"
    # ...without clobbering the client row's portal name.
    assert rows["client:1141:CS3001301"].course_name == "客戶端課程 CS3001301"
    # Only the insert of moodle:777 is a change; the client row is untouched.
    assert stats.changed_count == 1
