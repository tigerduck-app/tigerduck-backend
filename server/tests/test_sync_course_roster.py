"""A course upload's `roster_terms`: the school's roster of a term sets which
of its school courses the student is enrolled in, and `/sync/full` asks for
the current term's roster when none arrived for a week."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from server.academic_calendar.models import SemesterTerm
from server.auth.models import DevicePushToken, PushJob, UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.push.course_reminders import scan_course_reminders
from server.sync.merge import to_clock
from server.sync.models import UserCourse, UserCourseRoster

pytestmark = pytest.mark.asyncio(loop_scope="session")

TERM = "1141"
PAST = "1132"
PROTOCOL = {"X-TigerSync-Protocol": "2"}


def _key(course_no: str, term: str = TERM) -> str:
    return f"client:{term}:{course_no}"


async def _login(client, device: str = "iphone-abc") -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": device, "platform": "ios"},
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _upload(
    client, headers: dict, course_nos: list[str], *, term: str = TERM, schedule: dict | None = None,
    **extra,
) -> None:
    courses = [
        {"semester": term, "course_no": no, "course_name": no, "schedule_json": schedule or {}}
        for no in course_nos
    ]
    response = await client.post(
        "/v3/sync/courses/upload", headers=headers, json={"courses": courses, **extra}
    )
    assert response.status_code == 200, response.text


async def _roster(client, headers: dict, course_nos: list[str], term: str = TERM, **extra) -> None:
    await _upload(client, headers, course_nos, term=term, roster_terms=[term], **extra)


async def _add(client, headers: dict, course_no: str, at: datetime, **extra) -> None:
    add = [{"course_key": _key(course_no), "hlc": to_clock(at)}]
    await _upload(client, headers | PROTOCOL, [course_no], adds=add, **extra)


async def _statuses(client) -> dict[tuple[str, str], str]:
    async with build_session_factory(client.app.state.engine)() as session:
        rows = await session.execute(
            select(UserCourse.semester, UserCourse.course_no, UserCourse.enrollment_status)
        )
        return {(term, no): status for term, no, status in rows}


async def _write(client, statement) -> None:
    async with build_session_factory(client.app.state.engine)() as session:
        await session.execute(statement)
        await session.commit()


async def _mark_dropped(client, course_no: str) -> None:
    """As the operator marked stale rows by hand."""
    await _write(
        client,
        update(UserCourse).where(UserCourse.course_no == course_no).values(enrollment_status="dropped"),
    )


async def _publish_term(client) -> None:
    today = datetime.now(UTC).date()
    async with build_session_factory(client.app.state.engine)() as session:
        session.add(
            SemesterTerm(
                code=TERM, start_date=today - timedelta(days=30), end_date=today + timedelta(days=90)
            )
        )
        await session.commit()


async def _roster_requests(client, headers: dict) -> list[str]:
    response = await client.get("/v3/sync/full", headers=headers)
    assert response.status_code == 200
    return response.json()["roster_requests"]


# --- A roster sets its term's enrollment --------------------------------------


async def test_a_roster_drops_the_school_courses_it_does_not_list(client) -> None:
    phone = await _login(client)
    await _upload(client, phone, ["A", "B", "C"])

    await _roster(client, phone, ["A", "B"])

    assert await _statuses(client) == {
        (TERM, "A"): "enrolled", (TERM, "B"): "enrolled", (TERM, "C"): "dropped",
    }


async def test_a_roster_brings_a_dropped_course_back(client) -> None:
    phone = await _login(client)
    await _upload(client, phone, ["A", "B"])
    await _mark_dropped(client, "B")

    await _roster(client, phone, ["A", "B"])

    assert await _statuses(client) == {(TERM, "A"): "enrolled", (TERM, "B"): "enrolled"}


async def test_a_roster_leaves_hand_added_courses_and_other_terms(client) -> None:
    phone = await _login(client)
    now = datetime.now(UTC)
    await _upload(client, phone, ["A", "B", "HAND", "REMOVED"])
    await _upload(client, phone, ["OLD"], term=PAST)
    # Added by hand: one the school did not list, one it did.
    await _upload(client, phone, ["MINE"], force_keys=[_key("MINE")])
    await _add(client, phone, "HAND", now - timedelta(minutes=2))
    # A school course added by hand and taken back keeps its row beside a
    # removal tombstone.
    await _add(client, phone, "REMOVED", now - timedelta(minutes=2))
    response = await client.delete(
        f"/v3/sync/courses/{_key('REMOVED')}",
        headers=phone | PROTOCOL,
        params={"hlc": to_clock(now - timedelta(minutes=1)), "kind": "remove"},
    )
    assert response.json()["applied"] is True

    await _roster(client, phone, ["A"])

    assert await _statuses(client) == {
        (TERM, "A"): "enrolled",
        (TERM, "B"): "dropped",
        (TERM, "HAND"): "enrolled",
        (TERM, "MINE"): "enrolled",
        (TERM, "REMOVED"): "enrolled",
        (PAST, "OLD"): "enrolled",
    }


async def test_a_roster_drop_moves_the_revision_for_the_other_devices(client) -> None:
    phone = await _login(client)
    await _upload(client, phone, ["A", "B"])
    before = (await client.get("/v3/sync/revision", headers=phone)).json()["revision"]

    await _roster(client, phone, ["A"])

    assert (await client.get("/v3/sync/revision", headers=phone)).json()["revision"] > before


async def test_a_dropped_course_files_no_class_reminder_and_cancels_its_own(
    client, test_settings
) -> None:
    phone = await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        device = await session.scalar(select(UserDevice))
        session.add(
            DevicePushToken(
                device_id=device.id, provider="apns", token_kind="standard",
                token_hash="hash", token_value="tok",
            )
        )
        await session.commit()
    local = datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(hours=26)
    schedule = {str(local.isoweekday()): ["1"]}
    settings = test_settings.model_copy(
        update={"course_period_start_times": {"1": local.strftime("%H:%M")}}
    )

    async def reminders() -> dict[str, str]:
        async with factory() as session:
            jobs = await session.scalars(select(PushJob).where(PushJob.channel == "course"))
            return {job.payload["course_key"]: job.status for job in jobs}

    await _upload(client, phone, ["A", "B"], schedule=schedule)
    assert await scan_course_reminders(factory, settings) == 2

    await _roster(client, phone, ["A"], schedule=schedule)

    assert await scan_course_reminders(factory, settings) == 0
    assert await reminders() == {_key("A"): "pending", _key("B"): "cancelled"}


# --- Other uploads leave enrollment alone -------------------------------------


async def test_a_list_that_is_no_roster_keeps_a_dropped_course_dropped(client) -> None:
    phone = await _login(client)
    await _upload(client, phone, ["A", "B"])
    await _roster(client, phone, ["A"])
    assert (await _statuses(client))[(TERM, "B")] == "dropped"

    # A delete path's whole list, or a released app's.
    await _upload(client, phone, ["A", "B", "NEW"])

    assert await _statuses(client) == {
        (TERM, "A"): "enrolled", (TERM, "B"): "dropped", (TERM, "NEW"): "enrolled",
    }


@pytest.mark.parametrize("protocol_2", [True, False])
async def test_a_hand_add_brings_a_dropped_course_back(client, protocol_2: bool) -> None:
    phone = await _login(client)
    await _upload(client, phone, ["A", "B"])
    await _mark_dropped(client, "B")

    if protocol_2:
        await _add(client, phone, "B", datetime.now(UTC))
    else:
        await _upload(client, phone, ["B"], force_keys=[_key("B")])

    assert await _statuses(client) == {(TERM, "A"): "enrolled", (TERM, "B"): "enrolled"}


# --- A roster term with no school course is no roster -------------------------


async def test_a_roster_term_the_upload_lists_nothing_of_changes_nothing(client) -> None:
    phone = await _login(client)
    await _publish_term(client)
    await _upload(client, phone, ["A", "B"])
    await _upload(client, phone, ["X", "Y"], term=PAST)

    await _upload(client, phone, ["X"], term=PAST, roster_terms=[TERM, PAST])
    # An add with the term named lists no school course of it either.
    await _add(client, phone, "NEW", datetime.now(UTC), roster_terms=[TERM])

    assert await _statuses(client) == {
        (TERM, "A"): "enrolled", (TERM, "B"): "enrolled", (TERM, "NEW"): "enrolled",
        (PAST, "X"): "enrolled", (PAST, "Y"): "dropped",
    }
    assert await _roster_requests(client, phone) == [TERM]


async def test_a_roster_term_too_long_for_its_column_is_refused(client) -> None:
    phone = await _login(client)
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=phone,
        json={"courses": [], "roster_terms": ["1" * 17]},
    )
    assert response.status_code == 422


# --- Roster requests ----------------------------------------------------------


async def test_the_snapshot_asks_for_a_roster_that_is_missing_or_old(client) -> None:
    phone = await _login(client)
    assert await _roster_requests(client, phone) == []  # no term published

    await _publish_term(client)
    assert await _roster_requests(client, phone) == [TERM]
    await _upload(client, phone, ["A"])
    assert await _roster_requests(client, phone) == [TERM]  # no roster

    await _roster(client, phone, ["A"])
    assert await _roster_requests(client, phone) == []

    for age, requests in ((6, []), (8, [TERM])):
        await _write(
            client,
            update(UserCourseRoster).values(received_at=datetime.now(UTC) - timedelta(days=age)),
        )
        assert await _roster_requests(client, phone) == requests


async def test_turning_course_sync_off_forgets_when_the_roster_arrived(client) -> None:
    phone = await _login(client)
    await _publish_term(client)
    await _roster(client, phone, ["A"])
    assert await _roster_requests(client, phone) == []

    for on in (False, True):
        response = await client.patch(
            "/v3/devices/iphone-abc/preferences", headers=phone, json={"sync_courses": on}
        )
        assert response.status_code == 200, response.text
    assert await _roster_requests(client, phone) == [TERM]
