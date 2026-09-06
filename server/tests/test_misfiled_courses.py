"""The data migration statement that drops course rows filed under the
wrong term (server/sync/misfiled_courses.py)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.misfiled_courses import MISFILED_CLIENT_COURSES_DELETE
from server.sync.models import UserCourse

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


async def test_delete_keeps_rows_whose_moodle_id_matches_their_term(client) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    login = (await client.post("/v3/auth/login", json=LOGIN_BODY)).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    upload = await client.post("/v3/sync/courses/upload", headers=headers, json={"courses": [
        # The bug: a 115-1 enrolment filed under 114-2.
        {"semester": "1142", "course_no": "CS1", "course_name": "ghost", "moodle_id": "1151CS1"},
        # The same course correctly filed after the fix.
        {"semester": "1151", "course_no": "CS1", "course_name": "real", "moodle_id": "1151CS1"},
        # A manual course: the server derives moodle_id from its own term.
        {"semester": "1142", "course_no": "CS2", "course_name": "manual"},
        # A summer term, whose code is not all digits.
        {"semester": "114H", "course_no": "CS3", "course_name": "summer", "moodle_id": "114HCS3"},
        # A plain numeric Moodle id: not term-prefixed, must survive.
        {"semester": "1142", "course_no": "CS4", "course_name": "numeric", "moodle_id": "777"},
    ]})
    assert upload.status_code == 200

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        deleted = (await session.execute(MISFILED_CLIENT_COURSES_DELETE)).rowcount
        await session.commit()
        rows = (await session.execute(
            select(UserCourse.semester, UserCourse.course_no).where(
                UserCourse.user_id == uuid.UUID(login["user"]["id"])
            )
        )).all()

    assert deleted == 1
    assert sorted(rows) == [("1142", "CS2"), ("1142", "CS4"), ("114H", "CS3"), ("1151", "CS1")]
