"""End-to-end tests for the delta-sync override endpoints (/v3/sync/...)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import PushJob
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.merge import to_clock
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
    assert await assignment_marks(client) == [(synced_id, "none")]


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
    assert await assignment_marks(client) == [
        (placeholder_id, "none"),
        (synced_id, "none"),
    ]


async def test_clearing_a_mark_keeps_the_notes_of_the_assignment(client) -> None:
    """An override can also hold a note, which the initial upload sets. A
    clear takes the mark off every row and keeps the rows, so the clear's
    stamp outranks a mark made before it."""
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
        (other_id, "none"),
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


async def test_course_override_of_a_course_the_server_lacks_names_it(client) -> None:
    """An app can hold settings for a course the server no longer has: its
    rows go once no device syncs courses. The detail tells that 404 from
    the one a backend without the route gives."""
    login = await do_login(client)

    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"ignore_assignments": True},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "course_not_found"}
    assert await course_override_changes(client) == []
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None


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


async def test_clearing_a_course_name_reaches_the_other_devices(client) -> None:
    """The apps apply only the locales a snapshot lists, so a cleared one
    stays as "" rather than leaving the snapshot, and clearing the last one
    keeps the override that says so."""
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    async def rename(locale: str, name: str) -> dict:
        response = await client.patch(
            "/v3/sync/courses/1141CS3001301/override",
            headers=bearer(login),
            json={"custom_name": name, "locale": locale},
        )
        assert response.status_code == 200, response.text
        return response.json()["custom_names"]

    assert await rename("zh", "作業系統概論") == {"zh": "作業系統概論"}
    assert await rename("en", "OS") == {"zh": "作業系統概論", "en": "OS"}
    assert await rename("zh", "") == {"zh": "", "en": "OS"}
    assert await rename("en", "") == {"zh": "", "en": ""}

    snapshot = await client.get("/v3/sync/full", headers=bearer(login))
    assert [o["custom_names"] for o in snapshot.json()["course_overrides"]] == [
        {"zh": "", "en": ""}
    ]
    assert await course_override_changes(client) == [{"fields": ["custom_name"]}] * 4


# --- TigerSync stamps (`hlc`) -------------------------------------------------

PROTOCOL = {"X-TigerSync-Protocol": "2"}


def stamped(login: dict) -> dict:
    return {**bearer(login), **PROTOCOL}


def clock(ts: datetime) -> int:
    return to_clock(ts)


async def revision(client, login: dict) -> int:
    response = await client.get("/v3/sync/revision", headers=bearer(login))
    return response.json()["revision"]


async def mark(client, login: dict, status: str, at: datetime) -> dict:
    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=stamped(login),
        json={"local_status": status, "hlc": clock(at)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_mark_sent_late_loses_to_a_later_one(client) -> None:
    login = await do_login(client)
    synced_id = await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    t = datetime.now(UTC) - timedelta(hours=1)

    assert (await mark(client, login, "locally_completed", t + timedelta(minutes=30)))[
        "applied"
    ] is True
    before = (await revision(client, login), await sync_triggers(client))

    late = await mark(client, login, "ignored", t)
    assert late == {
        "id": late["id"],
        "updated_at": late["updated_at"],
        "local_status": "locally_completed",
        "local_status_hlc": clock(t + timedelta(minutes=30)),
        "applied": False,
    }
    assert await assignment_marks(client) == [(synced_id, "locally_completed")]
    assert (await revision(client, login), await sync_triggers(client)) == before


async def test_a_clear_outranks_a_mark_made_before_it(client) -> None:
    login = await do_login(client)
    synced_id = await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    t = datetime.now(UTC) - timedelta(hours=1)

    await mark(client, login, "ignored", t)
    cleared = await mark(client, login, "none", t + timedelta(minutes=10))
    assert cleared["deleted"] is True
    assert (cleared["local_status"], cleared["applied"]) == ("none", True)

    late = await mark(client, login, "ignored", t + timedelta(minutes=5))
    assert (late["local_status"], late["applied"]) == ("none", False)
    assert await assignment_marks(client) == [(synced_id, "none")]
    full = (await client.get("/v3/sync/full", headers=bearer(login))).json()
    assert [
        (o["local_status"], o["local_status_hlc"]) for o in full["assignment_overrides"]
    ] == [("none", clock(t + timedelta(minutes=10)))]


async def test_clearing_an_unmarked_assignment_records_the_clear_quietly(client) -> None:
    """A device can clear a mark the server never got; the clear's stamp is
    kept for that mark, and nothing changed for the other devices."""
    login = await do_login(client)
    synced_id = await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    t = datetime.now(UTC) - timedelta(hours=1)
    before = await revision(client, login)

    await mark(client, login, "none", t + timedelta(minutes=10))
    assert await revision(client, login) == before
    assert await sync_triggers(client) == 0

    assert (await mark(client, login, "ignored", t))["applied"] is False
    assert await assignment_marks(client) == [(synced_id, "none")]


async def test_the_same_mark_twice_moves_only_its_stamp(client) -> None:
    login = await do_login(client)
    await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    t = datetime.now(UTC) - timedelta(hours=1)

    await mark(client, login, "ignored", t)
    before = await revision(client, login)
    again = await mark(client, login, "ignored", t + timedelta(minutes=5))
    assert (again["applied"], again["local_status_hlc"]) == (
        True,
        clock(t + timedelta(minutes=5)),
    )
    assert await revision(client, login) == before
    # The moved stamp still outranks an edit made between the two.
    between = await mark(client, login, "locally_completed", t + timedelta(minutes=3))
    assert (between["local_status"], between["applied"]) == ("ignored", False)


async def test_an_app_that_stamps_its_edits_must_stamp_each(client) -> None:
    login = await do_login(client)
    await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )

    unstamped_mark = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=stamped(login),
        json={"local_status": "ignored"},
    )
    unstamped_colour = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=stamped(login),
        json={"color_hex": "#FF8800"},
    )
    assert (unstamped_mark.status_code, unstamped_colour.status_code) == (422, 422)
    assert unstamped_mark.json() == {"detail": "hlc_required"}
    assert await assignment_marks(client) == []
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(UserCourseOverride))).first() is None

    # The numeric Moodle id is routing data, not an edit.
    alone = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=stamped(login),
        json={"moodle_course_id": 4242},
    )
    assert alone.status_code == 200, alone.text


async def test_an_edit_without_a_stamp_is_stamped_when_it_arrives(client) -> None:
    """A released app sends no stamp: its edit counts from its arrival, so it
    beats an edit stamped earlier, as every edit did before stamps."""
    login = await do_login(client)
    await seed_assignment(client, login["user"]["id"], moodle_course_id=101)
    await mark(client, login, "locally_completed", datetime.now(UTC) - timedelta(minutes=1))

    sent_at = datetime.now(UTC)
    response = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "ignored"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert response.json()["local_status_hlc"] >= clock(sent_at)


async def test_a_stamp_from_the_future_is_clamped(client) -> None:
    login = await do_login(client)
    await seed_assignment(client, login["user"]["id"], moodle_course_id=101)

    sent_at = datetime.now(UTC)
    answer = await mark(client, login, "ignored", sent_at + timedelta(days=1))
    assert (
        clock(sent_at)
        <= answer["local_status_hlc"]
        <= clock(datetime.now(UTC) + timedelta(seconds=5))
    )


async def course_patch(client, login: dict, body: dict, at: datetime) -> dict:
    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=stamped(login),
        json={**body, "hlc": clock(at)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_course_fields_and_locales_resolve_on_their_own(client) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )
    t = datetime.now(UTC) - timedelta(hours=1)

    await course_patch(client, login, {"color_hex": "#FF0000"}, t + timedelta(minutes=10))
    await course_patch(
        client, login, {"custom_name": "作業系統", "locale": "zh"}, t + timedelta(minutes=10)
    )
    # A device that had not synced edits the name in English, and the colour,
    # both earlier: the name stays, the colour loses.
    answer = await course_patch(
        client,
        login,
        {"color_hex": "#0000FF", "custom_name": "OS", "locale": "en"},
        t,
    )
    assert answer["color_hex"] == "#FF0000"
    assert answer["custom_names"] == {"zh": "作業系統", "en": "OS"}
    assert answer["applied"] == ["custom_name"]
    assert answer["color_hex_hlc"] == clock(t + timedelta(minutes=10))
    assert answer["custom_names_hlc"] == {
        "zh": clock(t + timedelta(minutes=10)),
        "en": clock(t),
    }
    assert answer["ignore_assignments_hlc"] is None

    full = (await client.get("/v3/sync/full", headers=bearer(login))).json()
    (row,) = full["course_overrides"]
    assert (row["color_hex_hlc"], row["custom_names_hlc"]) == (
        answer["color_hex_hlc"],
        answer["custom_names_hlc"],
    )
    assert await course_override_changes(client) == [
        {"fields": ["color_hex"]},
        {"fields": ["custom_name"]},
        {"fields": ["custom_name"]},
    ]


async def test_the_same_colour_again_is_no_change(client) -> None:
    login = await do_login(client)
    await seed_course(
        client,
        login["user"]["id"],
        semester="1141",
        course_key="client:1141:CS3001301",
        moodle_id="1141CS3001301",
    )
    t = datetime.now(UTC) - timedelta(hours=1)

    await course_patch(client, login, {"color_hex": "#FF0000"}, t)
    triggers = await sync_triggers(client)
    again = await course_patch(client, login, {"color_hex": "#FF0000"}, t + timedelta(minutes=5))
    assert (again["applied"], again["color_hex_hlc"]) == (
        ["color_hex"],
        clock(t + timedelta(minutes=5)),
    )
    later = await course_patch(client, login, {"color_hex": "#0000FF"}, t + timedelta(minutes=3))
    assert (later["color_hex"], later["applied"]) == ("#FF0000", [])
    assert await course_override_changes(client) == [{"fields": ["color_hex"]}]
    assert await sync_triggers(client) == triggers
