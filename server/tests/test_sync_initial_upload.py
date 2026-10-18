"""End-to-end tests for POST /v3/sync/initial-upload."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from server.auth.models import PushJob
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.push.reminders import reminder_key_prefix
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
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


async def seed_course(client, login) -> None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            UserCourse(
                user_id=uuid.UUID(login["user"]["id"]),
                semester="1141",
                course_key="client:1141:CS3001301",
                course_no="CS3001301",
                course_name="作業系統",
                moodle_id="1141CS3001301",
            )
        )
        await session.commit()


async def upload_course_override(client, login, **fields):
    return await client.post(
        "/v3/sync/initial-upload",
        headers=bearer(login),
        json={
            "course_overrides": [
                {"semester": "1141", "course_key": "client:1141:CS3001301", **fields}
            ]
        },
    )


async def stored_override(client) -> UserCourseOverride:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (await session.execute(select(UserCourseOverride))).scalar_one()


async def course_override_changes(client) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(UserChangeLog)
                .where(UserChangeLog.entity_type == "course_override")
            )
        ).scalar_one()


async def ignore_on_the_server(client, login) -> datetime:
    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True},
    )
    assert response.status_code == 200, response.text
    return (await stored_override(client)).ignore_assignments_updated_at


async def test_initial_upload_carries_the_ignore_setting(client) -> None:
    login = await do_login(client)
    await seed_course(client, login)

    response = await upload_course_override(
        client,
        login,
        ignore_assignments=True,
        ignore_assignments_updated_at="2026-10-01T08:00:00+00:00",
        moodle_course_id=4242,
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["course_overrides"] == 1
    override = await stored_override(client)
    assert override.ignore_assignments is True
    assert override.ignore_assignments_updated_at == datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert str(override.ignore_assignments_device_id) == login["device_id"]
    assert override.moodle_course_id == 4242
    assert await course_override_changes(client) == 1


async def test_initial_upload_ignore_setting_needs_its_time(client) -> None:
    """An import carries a value for every course. Without a time a false
    would count as edited now and clear an ignore set on another device."""
    login = await do_login(client)
    await seed_course(client, login)
    ignored_at = await ignore_on_the_server(client, login)

    response = await upload_course_override(client, login, ignore_assignments=False)
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["course_overrides"] == 0
    override = await stored_override(client)
    assert override.ignore_assignments is True
    assert override.ignore_assignments_updated_at == ignored_at


@pytest.mark.parametrize("lead", [timedelta(days=1), timedelta(0)], ids=["older", "tie"])
async def test_initial_upload_ignore_setting_loses_to_a_newer_or_equal_one(
    client, lead
) -> None:
    login = await do_login(client)
    await seed_course(client, login)
    ignored_at = await ignore_on_the_server(client, login)

    response = await upload_course_override(
        client,
        login,
        ignore_assignments=False,
        ignore_assignments_updated_at=(ignored_at - lead).isoformat(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["course_overrides"] == 0
    override = await stored_override(client)
    assert override.ignore_assignments is True
    assert override.ignore_assignments_updated_at == ignored_at


async def test_initial_upload_ignore_setting_wins_over_an_older_one(client) -> None:
    login = await do_login(client)
    await seed_course(client, login)
    ignored_at = await ignore_on_the_server(client, login)

    response = await upload_course_override(
        client,
        login,
        ignore_assignments=False,
        ignore_assignments_updated_at=(ignored_at + timedelta(seconds=1)).isoformat(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["course_overrides"] == 1
    assert (await stored_override(client)).ignore_assignments is False


NAIVE_TIME = "2026-10-01T08:00:00"
UPLOADED_ASSIGNMENT = {"moodle_course_id": 101, "moodle_assignment_id": 9001, "title": "HW3"}


@pytest.mark.parametrize(
    "body",
    [
        {
            "course_overrides": [
                {
                    "semester": "1141",
                    "course_key": "client:1141:CS3001301",
                    "custom_name": "OS",
                    "custom_name_updated_at": NAIVE_TIME,
                }
            ]
        },
        {
            "course_overrides": [
                {
                    "semester": "1141",
                    "course_key": "client:1141:CS3001301",
                    "color_hex": "#FF8800",
                    "color_hex_updated_at": NAIVE_TIME,
                }
            ]
        },
        {
            "assignments": [UPLOADED_ASSIGNMENT],
            "assignment_overrides": [
                {
                    "moodle_course_id": 101,
                    "moodle_assignment_id": 9001,
                    "local_status": "ignored",
                    "local_status_updated_at": NAIVE_TIME,
                }
            ],
        },
        {
            "assignments": [UPLOADED_ASSIGNMENT],
            "assignment_overrides": [
                {
                    "moodle_course_id": 101,
                    "moodle_assignment_id": 9001,
                    "note": "bring a calculator",
                    "note_updated_at": NAIVE_TIME,
                }
            ],
        },
    ],
    ids=["custom-name", "color", "local-status", "note"],
)
async def test_initial_upload_refuses_a_time_without_a_zone(client, body) -> None:
    """An edit's time is compared with the server's clock, which has a zone,
    so a time without one cannot be placed against it."""
    login = await do_login(client)
    await seed_course(client, login)

    response = await client.post(
        "/v3/sync/initial-upload", headers=bearer(login), json=body
    )
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserChangeLog))).first() is None


async def test_initial_upload_fills_only_a_missing_moodle_course_id(client) -> None:
    login = await do_login(client)
    await seed_course(client, login)

    response = await upload_course_override(client, login, moodle_course_id=4242)
    assert response.json()["counts"]["course_overrides"] == 1
    assert (await stored_override(client)).moodle_course_id == 4242
    assert await course_override_changes(client) == 1

    response = await upload_course_override(client, login, moodle_course_id=999)
    assert response.json()["counts"]["course_overrides"] == 0
    assert (await stored_override(client)).moodle_course_id == 4242
    assert await course_override_changes(client) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {
            "ignore_assignments": True,
            "ignore_assignments_updated_at": "2026-10-01T08:00:00",
        },
        {"ignore_assignments": "yes"},
        {"ignore_assignments": 1},
        {"moodle_course_id": True},
        {"moodle_course_id": "4242"},
        {"moodle_course_id": 0},
        {"moodle_course_id": 2**63},
    ],
    ids=[
        "time-without-offset",
        "ignore-string",
        "ignore-number",
        "course-id-bool",
        "course-id-string",
        "course-id-zero",
        "course-id-over-bigint",
    ],
)
async def test_initial_upload_refuses_a_malformed_ignore_setting(client, fields) -> None:
    login = await do_login(client)
    await seed_course(client, login)

    response = await upload_course_override(client, login, **fields)
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None


async def seed_server_assignment(client, login, moodle_assignment_id: int) -> int:
    """The row the server's own Moodle fetch writes, under the real course id."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        row = UserAssignment(
            user_id=uuid.UUID(login["user"]["id"]),
            moodle_course_id=7001,
            moodle_assignment_id=moodle_assignment_id,
            title="HW5",
        )
        session.add(row)
        await session.commit()
        return row.id


async def rows_of(client, moodle_assignment_id: int) -> list[tuple[int, int]]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = await session.execute(
            select(UserAssignment.id, UserAssignment.moodle_course_id).where(
                UserAssignment.moodle_assignment_id == moodle_assignment_id
            )
        )
        return sorted(tuple(row) for row in rows)


async def test_initial_upload_files_an_apps_mark_on_the_servers_row(client) -> None:
    """The apps name no course (course id 0). A mark for an assignment the
    server fetched under its real course id goes on that row."""
    login = await do_login(client)
    server_row = await seed_server_assignment(client, login, 5)

    response = await client.post(
        "/v3/sync/initial-upload",
        headers=bearer(login),
        json={
            "assignment_overrides": [
                {
                    "moodle_course_id": 0,
                    "moodle_assignment_id": 5,
                    "local_status": "ignored",
                    "local_status_updated_at": "2026-10-01T08:00:00Z",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        marks = (
            await session.execute(
                select(
                    UserAssignmentOverride.user_assignment_id,
                    UserAssignmentOverride.local_status,
                )
            )
        ).all()
    assert [tuple(mark) for mark in marks] == [(server_row, "ignored")]


async def test_initial_upload_adds_no_second_row_for_an_assignment(client) -> None:
    """One row per assignment: an app's copy of one the server already has,
    under whatever course id, leaves it as it is."""
    login = await do_login(client)
    server_row = await seed_server_assignment(client, login, 5)

    response = await client.post(
        "/v3/sync/initial-upload",
        headers=bearer(login),
        json={
            "assignments": [
                {"moodle_course_id": 0, "moodle_assignment_id": 5, "title": "HW5"},
                {"moodle_course_id": 7002, "moodle_assignment_id": 5, "title": "HW5"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["assignments"] == 0
    assert await rows_of(client, 5) == [(server_row, 7001)]


async def upload_submission(client, login, *, submitted: bool, at: str | None = None):
    response = await client.post(
        "/v3/sync/initial-upload",
        headers=bearer(login),
        json={
            "assignments": [
                {
                    "moodle_course_id": 0,
                    "moodle_assignment_id": 5,
                    "title": "HW5",
                    "provider_is_submitted": submitted,
                    "provider_submitted_at": at,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


async def submission_of(client, moodle_assignment_id: int) -> list[tuple]:
    """(course id, submitted, submission time) of each row of the assignment."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = await session.execute(
            select(
                UserAssignment.moodle_course_id,
                UserAssignment.provider_is_submitted,
                UserAssignment.provider_submitted_at,
            ).where(UserAssignment.moodle_assignment_id == moodle_assignment_id)
        )
        return sorted(tuple(row) for row in rows)


async def test_initial_upload_reports_a_submission_on_the_servers_row(client) -> None:
    """The server's row keeps its own fields, but a submission the app saw
    reaches it, with its time. Moodle never takes one back, so a later
    upload that says otherwise leaves it."""
    login = await do_login(client)
    await seed_server_assignment(client, login, 5)

    await upload_submission(client, login, submitted=True, at="2026-10-01T08:00:00Z")
    await upload_submission(client, login, submitted=False)

    assert await submission_of(client, 5) == [
        (7001, True, datetime(2026, 10, 1, 8, tzinfo=UTC))
    ]


async def test_initial_upload_submission_cancels_the_pending_reminders(client) -> None:
    """As the regular assignment upload does: a reminder filed for the
    assignment goes at once instead of at the next scan."""
    login = await do_login(client)
    await seed_server_assignment(client, login, 5)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        reminder = PushJob(
            user_id=uuid.UUID(login["user"]["id"]),
            dedupe_key=f"{reminder_key_prefix(5)}24h:1790000000",
            channel="assignment",
            scenario="reminder_24h",
            fire_at=datetime.now(UTC) + timedelta(hours=1),
            payload={"kind": "assignment_reminder", "moodle_assignment_id": 5},
        )
        session.add(reminder)
        await session.commit()

    await upload_submission(client, login, submitted=True)

    async with factory() as session:
        assert (await session.get(PushJob, reminder.id)).status == "cancelled"
