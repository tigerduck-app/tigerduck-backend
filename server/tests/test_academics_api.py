"""End-to-end tests for /v3/courses and /v3/assignments."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserChangeLog,
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
            moodle_id=f"{semester}CS{semester}",
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


async def put_course_override(client, login: dict, course_id: int, **fields):
    return await client.put(
        f"/v3/courses/{course_id}/override", headers=bearer(login), json=fields
    )


def course_keys(override: dict) -> dict:
    return {key: override[key] for key in ("moodle_id", "course_no", "semester")}


async def test_course_routes_key_each_override_by_its_course(client) -> None:
    """Both apps key a course's override by the course's moodle_id, or by
    its semester and course_no, which the full sync sends with it."""
    login = await do_login(client)
    first_id = await seed_course(client, login["user"]["id"], "1141")
    second_id = await seed_course(client, login["user"]["id"], "1132")

    for course_id in (first_id, second_id):
        response = await put_course_override(
            client,
            login,
            course_id,
            color_hex="#FF8800",
            color_hex_updated_at="2026-06-08T10:00:00Z",
        )
        assert response.status_code == 200, response.text
    assert course_keys(response.json()["override"]) == {
        "moodle_id": "1132CS1132",
        "course_no": "CS1132",
        "semester": "1132",
    }

    listing = await client.get("/v3/courses", headers=bearer(login))
    assert {
        o["user_course_id"]: course_keys(o) for o in listing.json()["overrides"]
    } == {
        first_id: {"moodle_id": "1141CS1141", "course_no": "CS1141", "semester": "1141"},
        second_id: {"moodle_id": "1132CS1132", "course_no": "CS1132", "semester": "1132"},
    }


async def stored_course_override(client) -> UserCourseOverride:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (await session.execute(select(UserCourseOverride))).scalar_one()


async def course_override_changes(client) -> list[dict | None]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(UserChangeLog.payload)
                    .where(UserChangeLog.entity_type == "course_override")
                    .order_by(UserChangeLog.revision)
                )
            ).scalars()
        )


async def test_put_course_override_ignores_assignments(client) -> None:
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    response = await put_course_override(
        client,
        login,
        course_id,
        ignore_assignments=True,
        ignore_assignments_updated_at="2026-10-01T08:00:00Z",
        moodle_course_id=4242,
    )
    assert response.status_code == 200, response.text
    override = response.json()["override"]
    assert override["ignore_assignments"] is True
    assert override["ignore_assignments_updated_at"] == "2026-10-01T08:00:00+00:00"
    assert override["moodle_course_id"] == 4242
    stored = await stored_course_override(client)
    assert str(stored.ignore_assignments_device_id) == login["device_id"]

    # Merged per field like the name and colour: an older edit loses and a
    # newer one wins. The numeric id stays until a request sends another.
    for edited_at, ignored in (
        ("2026-09-30T08:00:00Z", True),
        ("2026-10-02T08:00:00Z", False),
    ):
        response = await put_course_override(
            client,
            login,
            course_id,
            ignore_assignments=False,
            ignore_assignments_updated_at=edited_at,
        )
        assert response.status_code == 200, response.text
        assert response.json()["override"]["ignore_assignments"] is ignored
    assert response.json()["override"]["moodle_course_id"] == 4242
    assert await course_override_changes(client) == [
        {"fields": ["ignore_assignments"]},
        {"fields": ["ignore_assignments"]},
    ]


async def test_put_course_override_takes_a_moodle_course_id_alone(client) -> None:
    """The id is for the server's reminder scan, so it alone changes nothing
    another device shows and sends no device to a full sync."""
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    response = await put_course_override(client, login, course_id, moodle_course_id=4242)
    assert response.status_code == 200, response.text
    stored = await stored_course_override(client)
    assert stored.moodle_course_id == 4242
    assert stored.ignore_assignments is False
    assert stored.ignore_assignments_updated_at is None
    assert await course_override_changes(client) == []


EDITED_AT = "2026-10-01T08:00:00Z"


@pytest.mark.parametrize(
    "fields",
    [
        {"ignore_assignments_updated_at": EDITED_AT},
        {"ignore_assignments": None, "ignore_assignments_updated_at": EDITED_AT},
        {"ignore_assignments": True, "ignore_assignments_updated_at": "2026-10-01T08:00:00"},
        {"ignore_assignments": "yes", "ignore_assignments_updated_at": EDITED_AT},
        {"ignore_assignments": 1, "ignore_assignments_updated_at": EDITED_AT},
        {"moodle_course_id": True},
        {"moodle_course_id": "4242"},
        {"moodle_course_id": 0},
        {"moodle_course_id": 2**63},
    ],
    ids=[
        "time-without-value",
        "null-value",
        "time-without-zone",
        "ignore-string",
        "ignore-number",
        "course-id-bool",
        "course-id-string",
        "course-id-zero",
        "course-id-over-bigint",
    ],
)
async def test_put_course_override_refuses_a_malformed_ignore_setting(
    client, fields
) -> None:
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    response = await put_course_override(client, login, course_id, **fields)
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None


NAIVE_TIME = "2026-06-08T10:00:00"


@pytest.mark.parametrize(
    "fields",
    [
        {"custom_name": "OS", "custom_name_updated_at": NAIVE_TIME},
        {"color_hex": "#FF8800", "color_hex_updated_at": NAIVE_TIME},
    ],
    ids=["custom-name", "color"],
)
async def test_put_course_override_refuses_a_time_without_a_zone(client, fields) -> None:
    """An edit's time is compared with the server's clock, which has a zone,
    so a time without one cannot be placed against it."""
    login = await do_login(client)
    course_id = await seed_course(client, login["user"]["id"])

    response = await client.put(
        f"/v3/courses/{course_id}/override", headers=bearer(login), json=fields
    )
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None


@pytest.mark.parametrize(
    "fields",
    [
        {"local_status": "ignored", "local_status_updated_at": NAIVE_TIME},
        {"note": "bring a calculator", "note_updated_at": NAIVE_TIME},
    ],
    ids=["local-status", "note"],
)
async def test_put_assignment_override_refuses_a_time_without_a_zone(
    client, fields
) -> None:
    login = await do_login(client)
    assignment_id = await seed_assignment(client, login["user"]["id"])

    response = await client.put(
        f"/v3/assignments/{assignment_id}/override", headers=bearer(login), json=fields
    )
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserAssignmentOverride))).first() is None


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
