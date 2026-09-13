"""End-to-end tests for /v3/courses and /v3/assignments."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import UserAssignment, UserChangeLog, UserCourse

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


async def do_login(client, student_id: str = "B11015000", device: str = "iphone-abc") -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    body = dict(LOGIN_BODY, student_id=student_id)
    body["device_info"] = dict(LOGIN_BODY["device_info"], client_device_id=device)
    response = await client.post("/v3/auth/login", json=body)
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def seed_course(client, user_id: str, semester: str = "1141") -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        course = UserCourse(
            user_id=uuid.UUID(user_id),
            semester=semester,
            course_key=f"CS{semester}",
            course_no=f"CS{semester}",
            course_name="作業系統",
        )
        session.add(course)
        await session.commit()
        return course.id


async def seed_assignment(client, user_id: str) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assignment = UserAssignment(
            user_id=uuid.UUID(user_id),
            moodle_course_id=101,
            moodle_assignment_id=9001,
            title="HW3",
        )
        session.add(assignment)
        await session.commit()
        return assignment.id


async def test_get_courses_filters_by_semester(client) -> None:
    login = await do_login(client)
    await seed_course(client, login["user"]["id"], "1141")
    await seed_course(client, login["user"]["id"], "1132")

    response = await client.get("/v3/courses?semester=1141", headers=bearer(login))
    assert response.status_code == 200
    body = response.json()
    assert len(body["courses"]) == 1
    assert body["courses"][0]["semester"] == "1141"

    everything = await client.get("/v3/courses", headers=bearer(login))
    assert len(everything.json()["courses"]) == 2


async def test_put_course_override_merges_per_field(client) -> None:
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    # Timestamps deliberately in the PAST relative to server time — future
    # timestamps get clamped to arrival time, which would turn this into a
    # last-write-wins test instead.
    first = await client.put(
        f"/v3/courses/{course_id}/override",
        headers=bearer(login),
        json={
            "color_hex": "#FF8800",
            "color_hex_updated_at": "2026-06-08T10:00:00Z",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["override"]["color_hex"] == "#FF8800"

    # An OLDER edit from another device must lose the color field...
    stale = await client.put(
        f"/v3/courses/{course_id}/override",
        headers=bearer(login),
        json={
            "color_hex": "#000000",
            "color_hex_updated_at": "2026-06-08T09:00:00Z",
            "custom_name": "OS",
            "custom_name_updated_at": "2026-06-08T09:30:00Z",
        },
    )
    assert stale.status_code == 200
    merged = stale.json()["override"]
    assert merged["color_hex"] == "#FF8800"  # newer server value kept
    # Untouched field applied. A locale-less custom_name feeds both 'zh'
    # and 'en' — the same convention as the a1b2c3d4e5f6 backfill.
    assert merged["custom_names"] == {"zh": "OS", "en": "OS"}


async def test_put_override_unknown_course_404(client) -> None:
    login = await do_login(client)
    response = await client.put(
        "/v3/courses/999999/override",
        headers=bearer(login),
        json={"color_hex": "#FF8800", "color_hex_updated_at": "2026-06-10T10:00:00Z"},
    )
    assert response.status_code == 404


async def test_put_override_other_users_course_404(client) -> None:
    login_a = await do_login(client)
    course_id = await seed_course(client, login_a["user"]["id"])
    login_b = await do_login(client, student_id="B11015999", device="other")
    response = await client.put(
        f"/v3/courses/{course_id}/override",
        headers=bearer(login_b),
        json={"color_hex": "#000000", "color_hex_updated_at": "2026-06-10T10:00:00Z"},
    )
    assert response.status_code == 404


async def test_skipped_dates_create_delete_recreate(client) -> None:
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    create = await client.put(
        f"/v3/courses/{course_id}/skipped-dates/2026-06-12",
        headers=bearer(login),
        json={"reason": "校慶"},
    )
    assert create.status_code == 200
    assert create.json()["skipped_on"] == "2026-06-12"

    listing = await client.get("/v3/courses", headers=bearer(login))
    assert len(listing.json()["skipped_dates"]) == 1

    delete = await client.delete(
        f"/v3/courses/{course_id}/skipped-dates/2026-06-12",
        headers=bearer(login),
    )
    assert delete.status_code == 204

    listing = await client.get("/v3/courses", headers=bearer(login))
    assert listing.json()["skipped_dates"] == []

    # Recreate after soft delete must work (undelete, not unique violation).
    recreate = await client.put(
        f"/v3/courses/{course_id}/skipped-dates/2026-06-12",
        headers=bearer(login),
        json={},
    )
    assert recreate.status_code == 200


async def test_get_assignments_with_override(client) -> None:
    login = await do_login(client)
    assignment_id = await seed_assignment(client, login["user"]["id"])

    put = await client.put(
        f"/v3/assignments/{assignment_id}/override",
        headers=bearer(login),
        json={
            "local_status": "locally_completed",
            "local_status_updated_at": "2026-06-10T10:00:00Z",
        },
    )
    assert put.status_code == 200
    assert put.json()["override"]["local_status"] == "locally_completed"

    response = await client.get("/v3/assignments", headers=bearer(login))
    body = response.json()
    assert len(body["assignments"]) == 1
    assert len(body["overrides"]) == 1
    assert body["overrides"][0]["local_status"] == "locally_completed"


async def test_override_writes_changelog(client) -> None:
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])
    await client.put(
        f"/v3/courses/{course_id}/override",
        headers=bearer(login),
        json={"color_hex": "#FF8800", "color_hex_updated_at": "2026-06-10T10:00:00Z"},
    )

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        entry = (
            await session.execute(
                select(UserChangeLog).where(
                    UserChangeLog.entity_type == "course_override"
                )
            )
        ).scalar_one()
        assert entry.entity_id == str(course_id)
        assert entry.payload == {"fields": ["color_hex"]}
