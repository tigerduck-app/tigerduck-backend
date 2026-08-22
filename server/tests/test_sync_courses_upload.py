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
