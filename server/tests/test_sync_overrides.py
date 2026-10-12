"""End-to-end tests for the delta-sync override endpoints (/v3/sync/...)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.auth.models import PushJob
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
    client,
    user_id: str,
    *,
    moodle_course_id: int,
    moodle_assignment_id: int = 9001,
    deleted_at: datetime | None = None,
) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assignment = UserAssignment(
            user_id=uuid.UUID(user_id),
            moodle_course_id=moodle_course_id,
            moodle_assignment_id=moodle_assignment_id,
            title="HW3",
            deleted_at=deleted_at,
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


async def assignment_marks(client) -> list[tuple[int, str]]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = await session.execute(
            select(
                UserAssignmentOverride.user_assignment_id,
                UserAssignmentOverride.local_status,
            ).order_by(UserAssignmentOverride.user_assignment_id)
        )
        return [tuple(row) for row in rows]


async def test_override_marks_an_assignment_whose_rows_are_all_soft_deleted(
    client,
) -> None:
    """The fetch leaves out a course the user is a TA in, but the apps still
    show its assignments. A mark on one used to 404, and the apps' outbox
    retried it on every full sync; the server-synced row takes it now."""
    login = await do_login(client)
    gone = datetime.now(UTC)
    await seed_assignment(
        client, login["user"]["id"], moodle_course_id=0, deleted_at=gone
    )
    synced_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=7002, deleted_at=gone
    )

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "ignored"},
    )
    assert response.status_code == 200, response.text
    assert await assignment_marks(client) == [(synced_id, "ignored")]
    full = (await client.get("/v3/sync/full", headers=bearer(login))).json()
    assert [
        (o["moodle_assignment_id"], o["local_status"])
        for o in full["assignment_overrides"]
    ] == [(9001, "ignored")]

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "none"},
    )
    assert response.status_code == 200, response.text
    assert await assignment_marks(client) == []


async def test_clearing_a_mark_clears_every_row_of_the_assignment(client) -> None:
    """A placeholder's mark outlives the fetch that soft-deletes the
    placeholder, and `/sync/full` keys it again once no row is live. A clear
    that left it would have the old mark come back."""
    login = await do_login(client)
    placeholder_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=0, deleted_at=datetime.now(UTC)
    )
    synced_id = await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add_all(
            [
                UserAssignmentOverride(
                    user_id=uuid.UUID(login["user"]["id"]),
                    user_assignment_id=placeholder_id,
                    local_status="ignored",
                ),
                UserAssignmentOverride(
                    user_id=uuid.UUID(login["user"]["id"]),
                    user_assignment_id=synced_id,
                    local_status="locally_completed",
                ),
            ]
        )
        await session.commit()

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "none"},
    )
    assert response.status_code == 200, response.text
    assert await assignment_marks(client) == []


async def test_clearing_a_mark_keeps_the_notes_of_the_assignment(client) -> None:
    """An override can also hold a note, which the initial upload sets. A
    clear takes the mark off such a row and deletes only rows with nothing
    else saved."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    placeholder_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=0, deleted_at=datetime.now(UTC)
    )
    synced_id = await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    other_id = await seed_assignment(
        client, login["user"]["id"], moodle_course_id=102, deleted_at=datetime.now(UTC)
    )
    noted_at = datetime(2026, 10, 1, tzinfo=UTC)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add_all(
            [
                UserAssignmentOverride(
                    user_id=user_id,
                    user_assignment_id=placeholder_id,
                    local_status="ignored",
                    note="bring a calculator",
                    note_updated_at=noted_at,
                ),
                UserAssignmentOverride(
                    user_id=user_id,
                    user_assignment_id=synced_id,
                    local_status="locally_completed",
                    note_updated_at=noted_at,
                ),
                UserAssignmentOverride(
                    user_id=user_id, user_assignment_id=other_id, local_status="ignored"
                ),
            ]
        )
        await session.commit()

    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "none"},
    )
    assert response.status_code == 200, response.text
    assert await assignment_marks(client) == [
        (placeholder_id, "none"),
        (synced_id, "none"),
    ]
    async with factory() as session:
        kept = {
            o.user_assignment_id: o
            for o in (await session.execute(select(UserAssignmentOverride))).scalars()
        }
    assert kept[placeholder_id].note == "bring a calculator"
    assert kept[placeholder_id].note_updated_at == noted_at
    assert kept[synced_id].note is None
    assert kept[synced_id].note_updated_at == noted_at
    assert str(kept[synced_id].local_status_device_id) == login["device_id"]
    assert kept[synced_id].local_status_updated_at > noted_at
    full = (await client.get("/v3/sync/full", headers=bearer(login))).json()
    assert {o["local_status"] for o in full["assignment_overrides"]} == {"none"}


async def test_override_of_an_assignment_the_user_never_had_is_404(client) -> None:
    login = await do_login(client)
    await seed_assignment(client, login["user"]["id"], moodle_course_id=101)

    response = await client.patch(
        "/v3/sync/assignments/9002/override",
        headers=bearer(login),
        json={"local_status": "ignored"},
    )
    assert response.status_code == 404
    assert await assignment_marks(client) == []


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


async def course_override(client) -> UserCourseOverride:
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


async def test_course_override_ignores_assignments_and_stops(client) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True, "moodle_course_id": 4242},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ignore_assignments"] is True
    override = await course_override(client)
    assert override.ignore_assignments is True
    assert override.ignore_assignments_updated_at is not None
    assert str(override.ignore_assignments_device_id) == login["device_id"]
    assert override.moodle_course_id == 4242
    set_at = override.ignore_assignments_updated_at

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ignore_assignments"] is False
    override = await course_override(client)
    assert override.ignore_assignments is False
    assert override.ignore_assignments_updated_at > set_at
    # Left out of the request, the numeric id stays.
    assert override.moodle_course_id == 4242
    assert await course_override_changes(client) == [
        {"fields": ["ignore_assignments"]},
        {"fields": ["ignore_assignments"]},
    ]


async def test_course_override_without_ignore_assignments_keeps_it(client) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )
    await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True, "moodle_course_id": 4242},
    )
    set_at = (await course_override(client)).ignore_assignments_updated_at

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"color_hex": "#FF8800"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ignore_assignments"] is True
    override = await course_override(client)
    assert override.ignore_assignments is True
    assert override.ignore_assignments_updated_at == set_at
    assert override.moodle_course_id == 4242
    assert override.color_hex == "#FF8800"
    assert (await course_override_changes(client))[-1] == {"fields": ["color_hex"]}


async def sync_triggers(client) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return len(
            (
                await session.execute(
                    select(PushJob).where(PushJob.scenario == "sync_trigger")
                )
            )
            .scalars()
            .all()
        )


async def test_course_override_takes_a_moodle_course_id_alone(client) -> None:
    """A client that learns the numeric id after ignoring the course sends
    only the id; that changes no field another device shows, so no device
    is sent to a full sync."""
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"moodle_course_id": 4242},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ignore_assignments"] is False
    override = await course_override(client)
    assert override.moodle_course_id == 4242
    assert override.ignore_assignments is False
    assert override.ignore_assignments_updated_at is None
    assert await course_override_changes(client) == []
    assert await sync_triggers(client) == 0


@pytest.mark.parametrize(
    "body",
    [{}, {"ignore_assignments": None, "moodle_course_id": None}, {"custom_name": "OS"}],
    ids=["empty", "nulls", "name-without-locale"],
)
async def test_course_override_that_changes_nothing_starts_no_sync(client, body) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json=body,
    )
    assert response.status_code == 200, response.text
    assert await course_override_changes(client) == []
    assert await sync_triggers(client) == 0


async def test_course_override_change_starts_a_sync(client) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True, "moodle_course_id": 4242},
    )
    assert response.status_code == 200, response.text
    assert await course_override_changes(client) == [{"fields": ["ignore_assignments"]}]
    assert await sync_triggers(client) == 1


@pytest.mark.parametrize("moodle_course_id", [0, -1, 2**63])
async def test_course_override_rejects_a_moodle_course_id_out_of_range(
    client, moodle_course_id
) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True, "moodle_course_id": moodle_course_id},
    )
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None


@pytest.mark.parametrize(
    "body",
    [
        {"ignore_assignments": "yes"},
        {"ignore_assignments": "true"},
        {"ignore_assignments": 1},
        {"moodle_course_id": True},
        {"moodle_course_id": "4242"},
        {"moodle_course_id": 4242.0},
    ],
    ids=[
        "ignore-yes",
        "ignore-quoted-true",
        "ignore-one",
        "course-id-true",
        "course-id-quoted",
        "course-id-float",
    ],
)
async def test_course_override_refuses_a_value_of_the_wrong_type(client, body) -> None:
    """A lax model took "yes" and 1 as true and stored true as course 1."""
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json=body,
    )
    assert response.status_code == 422
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None
