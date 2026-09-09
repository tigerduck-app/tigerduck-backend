"""POST /v3/sync/courses/upload: course-override (color) merge semantics."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import UserCourseOverride

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


def _course(course_no: str, name: str) -> dict:
    return {"semester": "1132", "course_no": course_no, "course_name": name}


async def _override_colors(client, user_id: str) -> list[str | None]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(UserCourseOverride).where(
                        UserCourseOverride.user_id == uuid.UUID(user_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        return sorted(r.color_hex or "" for r in rows)


async def test_overrides_created_skipped_and_first_write_wins(client) -> None:
    login = await do_login(client)

    response = await client.post(
        "/v3/sync/courses/upload",
        headers=bearer(login),
        json={
            "courses": [
                _course("CS0123", "資料結構"),
                _course("EE0456", "電路學"),
            ],
            "course_overrides": [
                {"course_key": "client:1132:CS0123", "color_hex": "#FF8800"},
                # No color: ignored entirely.
                {"course_key": "client:1132:EE0456"},
                # Unknown course: skipped silently.
                {"course_key": "client:1132:ZZ9999", "color_hex": "#123456"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["upserted"] == 2
    assert body["overrides_applied"] == 1
    assert await _override_colors(client, login["user"]["id"]) == ["#FF8800"]

    # Re-upload: server color wins over a conflicting client color, while a
    # course that never had one accepts its first color.
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=bearer(login),
        json={
            "courses": [
                _course("CS0123", "資料結構"),
                _course("EE0456", "電路學"),
            ],
            "course_overrides": [
                {"course_key": "client:1132:CS0123", "color_hex": "#000000"},
                {"course_key": "client:1132:EE0456", "color_hex": "#00AA55"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["overrides_applied"] == 1
    assert await _override_colors(client, login["user"]["id"]) == [
        "#00AA55",
        "#FF8800",
    ]


async def _upload(client, login: dict, courses: list[dict]) -> None:
    response = await client.post(
        "/v3/sync/courses/upload",
        json={"courses": courses},
        headers=bearer(login),
    )
    assert response.status_code == 200


async def _snapshot(client, login: dict) -> dict:
    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    return response.json()


async def test_delete_courses_scoped_to_semester_leaves_other_terms(client) -> None:
    login = await do_login(client)
    await _upload(client, login, [
        {"semester": "1132", "course_no": "CS101", "course_name": "Old"},
        {"semester": "1141", "course_no": "CS101", "course_name": "Retake"},
        {"semester": "1141", "course_no": "CS202", "course_name": "New"},
    ])
    # A per-course delete leaves a tombstone that binds its own author. The
    # semester reset turns it into a reset tombstone of this device's, so
    # the reset device's re-upload of CS101 at the end of this test is
    # released rather than skipped, while every other device stays bound.
    tomb = await client.delete("/v3/sync/courses/client:1132:CS101", headers=bearer(login))
    assert tomb.json() == {"deleted": 1}
    await _upload(client, login, [{"semester": "1132", "course_no": "CS303", "course_name": "Kept"}])

    response = await client.delete("/v3/sync/courses", params={"semester": "1132"}, headers=bearer(login))
    assert response.json() == {"deleted": 1}

    snap = await _snapshot(client, login)
    assert sorted((c["semester"], c["course_no"]) for c in snap["courses"]) == [
        ("1141", "CS101"), ("1141", "CS202"),
    ]
    # The reset records what it dropped, so a device that has not reconciled
    # yet cannot upload the term straight back. Reset tombstones bind every
    # device except the one that wrote them -- the CS101 round trip below is
    # that exemption.
    assert sorted(t["course_key"] for t in snap["course_tombstones"]) == [
        "client:1132:CS101", "client:1132:CS303",
    ]
    assert all(t["deleted_by_reset"] for t in snap["course_tombstones"])
    # Only a full reset may flag courses_reset_at — other devices wipe every
    # semester's local overlay when they see it.
    assert snap["courses_reset_at"] is None

    await _upload(client, login, [{"semester": "1132", "course_no": "CS101", "course_name": "Back"}])
    snap = await _snapshot(client, login)
    assert ("1132", "CS101") in {(c["semester"], c["course_no"]) for c in snap["courses"]}


async def test_delete_all_courses_without_semester_flags_full_reset(client) -> None:
    login = await do_login(client)
    await _upload(client, login, [
        {"semester": "1132", "course_no": "CS101", "course_name": "Old"},
        {"semester": "1141", "course_no": "CS202", "course_name": "New"},
    ])
    response = await client.delete("/v3/sync/courses", headers=bearer(login))
    assert response.json() == {"deleted": 2}
    snap = await _snapshot(client, login)
    assert snap["courses"] == []
    assert snap["courses_reset_at"] is not None
    # A full reset leaves no tombstones. courses_reset_at already tells every
    # device to drop its overlay, and a marker on each course would outlive
    # that: nothing re-uploads a term the student has finished, so those
    # timetables would never come back -- here included.
    assert snap["course_tombstones"] == []
