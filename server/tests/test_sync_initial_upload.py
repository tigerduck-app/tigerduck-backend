"""End-to-end tests for POST /v3/sync/initial-upload."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import (
    UserAssignment,
    UserBulletinSubscription,
    UserChangeLog,
    UserCourse,
    UserCourseOverride,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}

UPLOAD_BODY = {
    "courses": [
        {
            "semester": "1141",
            "course_key": "CS3001301",
            "course_no": "CS3001301",
            "course_name": "作業系統",
            "instructors": ["王老師"],
            "schedule_json": [{"day": 1, "periods": [3, 4]}],
        },
        {
            "semester": "1141",
            "course_key": "manual:3f9e",
            "source": "user_added",
            "course_name": "自訂讀書會",
        },
    ],
    "course_overrides": [
        {
            "semester": "1141",
            "course_key": "CS3001301",
            "color_hex": "#FF8800",
            "color_hex_updated_at": "2026-06-09T10:00:00Z",
        }
    ],
    "course_skipped_dates": [
        {
            "semester": "1141",
            "course_key": "CS3001301",
            "skipped_on": "2026-06-12",
        }
    ],
    "assignments": [
        {
            "moodle_course_id": 101,
            "moodle_assignment_id": 9001,
            "title": "HW3",
            "due_at": "2026-06-20T23:59:00Z",
        }
    ],
    "assignment_overrides": [
        {
            "moodle_course_id": 101,
            "moodle_assignment_id": 9001,
            "local_status": "locally_completed",
            "local_status_updated_at": "2026-06-09T11:00:00Z",
        }
    ],
    "settings_documents": [
        {"namespace": "appearance", "document": {"theme": "dark"}}
    ],
    # Sent by clients that predate per-device subscriptions; bulletins are
    # not part of TigerSync, so the upload must ignore it.
    "bulletin_subscriptions": [
        {"name": "教務處", "orgs": ["教務處"], "tags": [], "mode": "AND"}
    ],
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


async def test_initial_upload_creates_everything(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=UPLOAD_BODY
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["courses"] == 2
    assert body["counts"]["assignments"] == 1
    assert body["counts"]["settings_documents"] == 1
    assert "bulletin_subscriptions" not in body["counts"]
    assert body["current_revision"] > 0

    factory = build_session_factory(client.app.state.engine)
    user_id = uuid.UUID(login["user"]["id"])
    async with factory() as session:
        courses = (
            (await session.execute(select(UserCourse))).scalars().all()
        )
        assert len(courses) == 2
        override = (
            await session.execute(select(UserCourseOverride))
        ).scalar_one()
        assert override.color_hex == "#FF8800"
        assert override.color_hex_updated_at is not None

        changelog_count = (
            await session.execute(
                select(func.count())
                .select_from(UserChangeLog)
                .where(UserChangeLog.user_id == user_id)
            )
        ).scalar_one()
        # courses(2) + override(1) + skipped(1) + assignment(1) +
        # assignment_override(1) + settings(1) = 7
        assert changelog_count == 7


async def test_initial_upload_is_idempotent(client) -> None:
    login = await do_login(client)
    first = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=UPLOAD_BODY
    )
    assert first.status_code == 200
    second = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=UPLOAD_BODY
    )
    assert second.status_code == 200

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (
            await session.execute(
                select(func.count()).select_from(UserCourse)
            )
        ).scalar_one() == 2
        assert (
            await session.execute(
                select(func.count()).select_from(UserAssignment)
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count()).select_from(UserBulletinSubscription)
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                select(func.count()).select_from(UserSettingsDocument)
            )
        ).scalar_one() == 1


async def test_existing_settings_namespace_is_preserved(client) -> None:
    login = await do_login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            UserSettingsDocument(
                user_id=uuid.UUID(login["user"]["id"]),
                namespace="appearance",
                document={"theme": "light"},
                revision=5,
            )
        )
        await session.commit()

    response = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=UPLOAD_BODY
    )
    assert response.status_code == 200

    async with factory() as session:
        doc = (
            await session.execute(select(UserSettingsDocument))
        ).scalar_one()
        # Server copy (another device's upload) is authoritative.
        assert doc.document == {"theme": "light"}
        assert doc.revision == 5


async def test_override_for_unknown_course_is_skipped(client) -> None:
    login = await do_login(client)
    body = {
        "course_overrides": [
            {
                "semester": "1141",
                "course_key": "GHOST",
                "color_hex": "#000000",
                "color_hex_updated_at": "2026-06-09T10:00:00Z",
            }
        ]
    }
    response = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=body
    )
    assert response.status_code == 200
    assert response.json()["counts"]["course_overrides"] == 0
