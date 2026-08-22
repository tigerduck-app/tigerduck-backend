"""End-to-end tests for the delta-sync override endpoints (/v3/sync/...)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


async def do_login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def seed_assignment(
    client, user_id: str, *, moodle_course_id: int, moodle_assignment_id: int = 9001
) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assignment = UserAssignment(
            user_id=uuid.UUID(user_id),
            moodle_course_id=moodle_course_id,
            moodle_assignment_id=moodle_assignment_id,
            title="HW3",
        )
        session.add(assignment)
        await session.commit()
        return assignment.id


async def test_override_prefers_server_synced_row_over_upload_placeholder(client) -> None:
    """Server sync and client upload can both hold a live row for the same
    moodle_assignment_id (uploads use moodle_course_id=0, fetches the real
    course ID). The override must not 500 on the duplicate and must attach
    to the server-synced row, which survives the next fetch."""
    login = await do_login(client)
    synced_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=101
    )
    await seed_assignment(client, login["user"]["id"], moodle_course_id=0)

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "locally_completed"},
    )
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        override = (
            await session.execute(select(UserAssignmentOverride))
        ).scalar_one()
        assert override.user_assignment_id == synced_id
        assert override.local_status == "locally_completed"


async def seed_course(
    client,
    user_id: str,
    *,
    semester: str,
    course_key: str,
    moodle_id: str,
) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        course = UserCourse(
            user_id=uuid.UUID(user_id),
            semester=semester,
            course_key=course_key,
            course_no="CS3001301",
            course_name="作業系統",
            moodle_id=moodle_id,
        )
        session.add(course)
        await session.commit()
        return course.id


async def test_course_override_prefers_portal_row_over_moodle_shell(client) -> None:
    """A Moodle fetch creates a shell row (semester="", course_key
    "moodle:<id>") and a client portal upload can carry the same moodle_id
    on its own course_key, so two live rows can share a moodle_id. The
    override must not 500 and must attach to the portal row the user
    actually sees."""
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="",
        course_key="moodle:777",
        moodle_id="777",
    )
    portal_id = await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="777",
    )

    response = await client.patch(
        "/v3/sync/courses/777/override",
        headers=bearer(login),
        json={"color_hex": "#FF8800"},
    )
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        override = (
            await session.execute(select(UserCourseOverride))
        ).scalar_one()
        assert override.user_course_id == portal_id
        assert override.color_hex == "#FF8800"


async def test_override_on_upload_placeholder_only(client) -> None:
    """Without a server-synced row (cloud sync off), the upload placeholder
    itself takes the override."""
    login = await do_login(client)
    placeholder_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=0
    )

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "ignored"},
    )
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        override = (
            await session.execute(select(UserAssignmentOverride))
        ).scalar_one()
        assert override.user_assignment_id == placeholder_id
        assert override.local_status == "ignored"
