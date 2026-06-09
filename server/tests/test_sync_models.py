"""Round-trip and constraint tests for the Phase-2 sync tables."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.auth.models import User
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def make_user(db_session) -> User:
    user = User(student_id="B11015000")
    db_session.add(user)
    await db_session.flush()
    return user


async def test_course_with_override_and_skipped_date_round_trip(db_session) -> None:
    user = await make_user(db_session)
    course = UserCourse(
        user_id=user.id,
        semester="1141",
        course_key="CS3001301",
        course_no="CS3001301",
        course_name="作業系統",
        instructors=["王老師"],
        schedule_json=[{"day": 1, "periods": [3, 4]}],
    )
    db_session.add(course)
    await db_session.flush()

    db_session.add(
        UserCourseOverride(
            user_id=user.id,
            user_course_id=course.id,
            color_hex="#FF8800",
            color_hex_updated_at=datetime.now(UTC),
        )
    )
    db_session.add(
        UserCourseSkippedDate(
            user_id=user.id,
            user_course_id=course.id,
            skipped_on=date(2026, 6, 12),
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UserCourse).where(UserCourse.course_key == "CS3001301")
        )
    ).scalar_one()
    assert row.source == "ntust_portal"
    assert row.enrollment_status == "enrolled"
    assert row.schedule_json == [{"day": 1, "periods": [3, 4]}]


async def test_course_unique_per_user_semester_key(db_session) -> None:
    user = await make_user(db_session)
    for _ in range(2):
        db_session.add(
            UserCourse(
                user_id=user.id,
                semester="1141",
                course_key="CS3001301",
                course_no="CS3001301",
                course_name="作業系統",
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_portal_course_requires_course_no(db_session) -> None:
    user = await make_user(db_session)
    db_session.add(
        UserCourse(
            user_id=user.id,
            semester="1141",
            course_key="CS3001301",
            course_no=None,  # violates chk_course_source_key
            course_name="作業系統",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_assignment_with_override_round_trip(db_session) -> None:
    user = await make_user(db_session)
    assignment = UserAssignment(
        user_id=user.id,
        moodle_course_id=101,
        moodle_assignment_id=9001,
        title="HW3",
        due_at=datetime(2026, 6, 20, 23, 59, tzinfo=UTC),
    )
    db_session.add(assignment)
    await db_session.flush()

    db_session.add(
        UserAssignmentOverride(
            user_id=user.id,
            user_assignment_id=assignment.id,
            local_status="locally_completed",
            local_status_updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UserAssignmentOverride).where(
                UserAssignmentOverride.user_assignment_id == assignment.id
            )
        )
    ).scalar_one()
    assert row.local_status == "locally_completed"
    assert row.note is None


async def test_assignment_unique_per_user_moodle_ids(db_session) -> None:
    user = await make_user(db_session)
    for _ in range(2):
        db_session.add(
            UserAssignment(
                user_id=user.id,
                moodle_course_id=101,
                moodle_assignment_id=9001,
                title="HW3",
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.commit()
