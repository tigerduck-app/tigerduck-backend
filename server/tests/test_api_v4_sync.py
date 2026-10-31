"""The `/v4` forms of the sync endpoints (docs/api-v4.md, "Changed
endpoints"): the upload, both override PATCHes, the snapshot and the feed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from server.api_v4 import CODES
from server.db import build_session_factory
from server.sync.merge import to_clock
from server.sync.models import UserCourseOverride, UserSyncState
from server.tests.test_api_v4 import bearer, login
from server.tests.test_sync_api import seed_changes
from server.tests.test_sync_overrides import seed_assignment

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEMESTER = "1141"


def key(course_no: str, semester: str = SEMESTER) -> str:
    return f"client:{semester}:{course_no}"


def course(course_no: str, semester: str = SEMESTER, **fields) -> dict:
    return {"semester": semester, "course_no": course_no, "course_name": course_no, **fields}


@pytest.fixture
def t() -> datetime:
    return datetime.now(UTC) - timedelta(hours=2)


async def upload(client, headers, body: dict, prefix: str = "/v4"):
    return await client.post(f"{prefix}/sync/courses/upload", headers=headers, json=body)


# --- Course upload --------------------------------------------------------------


async def test_the_upload_refuses_course_overrides_and_force_keys(client) -> None:
    session = await login(client)
    for extra in (
        {"course_overrides": [{"course_key": key("CS1"), "color_hex": "#FF0000"}]},
        {"force_keys": []},
    ):
        response = await upload(client, bearer(session), {"courses": [course("CS1")], **extra})
        assert response.status_code == 422, extra
        assert response.json()["error"] == "invalid_request"
        assert response.json()["fields"][0]["loc"] == ["body"]


async def test_the_upload_answers_its_adds_and_writes_no_colour(client, t) -> None:
    session = await login(client)
    response = await upload(
        client,
        bearer(session),
        {
            "courses": [course("CS1"), course("CS2")],
            "adds": [{"course_key": key("CS2"), "hlc": to_clock(t)}],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "upserted": 2,
        "skipped_tombstoned": 0,
        "accepted_keys": [key("CS1"), key("CS2")],
        "adds": [{"course_key": key("CS2"), "applied": True, "hlc": to_clock(t)}],
    }
    async with build_session_factory(client.app.state.engine)() as db:
        assert await db.scalar(select(func.count()).select_from(UserCourseOverride)) == 0


async def test_an_add_outside_courses_is_add_not_in_courses(client, t) -> None:
    session = await login(client)
    response = await upload(
        client,
        bearer(session),
        {"courses": [course("CS1")], "adds": [{"course_key": key("CS9"), "hlc": to_clock(t)}]},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": "add_not_in_courses", "message": CODES["add_not_in_courses"],
    }


# --- Course override by key -------------------------------------------------------


async def test_the_course_override_finds_the_course_by_its_key(client, t) -> None:
    """Two terms of one course share a Moodle id, which names no term; the
    key does."""
    session = await login(client)
    await upload(
        client,
        bearer(session),
        {"courses": [course("CS1", moodle_id="M1"), course("CS1", "1142", moodle_id="M1")]},
    )

    response = await client.patch(
        f"/v4/sync/courses/{key('CS1', '1142')}/override",
        headers=bearer(session),
        json={"color_hex": "#00FF00", "hlc": to_clock(t)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body.keys() == {
        "id", "color_hex", "custom_names", "ignore_assignments", "updated_at",
        "color_hex_hlc", "custom_names_hlc", "ignore_assignments_hlc", "applied",
    }
    assert (body["color_hex"], body["color_hex_hlc"], body["applied"]) == (
        "#00FF00", to_clock(t), ["color_hex"],
    )
    snapshot = (await client.get("/v4/sync/full", headers=bearer(session))).json()
    assert [(o["course_key"], o["color_hex"]) for o in snapshot["course_overrides"]] == [
        (key("CS1", "1142"), "#00FF00")
    ]


async def test_the_course_override_answers_unknown_keys_and_unstamped_edits(client) -> None:
    session = await login(client)
    await upload(client, bearer(session), {"courses": [course("CS1")]})

    missing = await client.patch(
        f"/v4/sync/courses/{key('CS9')}/override",
        headers=bearer(session),
        json={"color_hex": "#00FF00", "hlc": to_clock(datetime.now(UTC))},
    )
    unstamped = await client.patch(
        f"/v4/sync/courses/{key('CS1')}/override",
        headers=bearer(session),
        json={"color_hex": "#00FF00"},
    )

    assert (missing.status_code, missing.json()["error"]) == (404, "course_not_found")
    assert (unstamped.status_code, unstamped.json()["error"]) == (422, "hlc_required")


async def test_any_course_override_value_needs_an_hlc_on_v4_only(client) -> None:
    session = await login(client)
    await upload(client, bearer(session), {"courses": [course("CS1")]})
    for body in (
        {"color_hex": "#00FF00"},
        {"custom_name": "Algorithms"},
        {"locale": "en"},
        {"ignore_assignments": True},
        {"moodle_course_id": 42},
    ):
        v4 = await client.patch(
            f"/v4/sync/courses/{key('CS1')}/override", headers=bearer(session), json=body
        )
        assert (v4.status_code, v4.json()["error"]) == (422, "hlc_required"), body
        v3 = await client.patch(
            f"/v3/sync/courses/{SEMESTER}CS1/override", headers=bearer(session), json=body
        )
        assert v3.status_code == 200, body

    stamped = await client.patch(
        f"/v4/sync/courses/{key('CS1')}/override",
        headers=bearer(session),
        json={"moodle_course_id": 42, "hlc": to_clock(datetime.now(UTC))},
    )
    assert stamped.status_code == 200, stamped.text


# --- Assignment override ----------------------------------------------------------


async def test_every_assignment_override_reply_has_one_shape(client, t) -> None:
    session = await login(client)
    await seed_assignment(client, session["user"]["id"], moodle_course_id=101)
    path = "/v4/sync/assignments/9001/override"

    mark = await client.patch(
        path, headers=bearer(session), json={"local_status": "ignored", "hlc": to_clock(t)}
    )
    clear = await client.patch(
        path,
        headers=bearer(session),
        json={"local_status": "none", "hlc": to_clock(t + timedelta(minutes=1))},
    )
    late = await client.patch(
        path, headers=bearer(session), json={"local_status": "archived", "hlc": to_clock(t)}
    )
    unstamped = await client.patch(path, headers=bearer(session), json={"local_status": "none"})

    assert mark.json() == {"local_status": "ignored", "local_status_hlc": to_clock(t), "applied": True}
    assert clear.json() == {
        "local_status": "none",
        "local_status_hlc": to_clock(t + timedelta(minutes=1)),
        "applied": True,
    }
    assert late.json() == {
        "local_status": "none",
        "local_status_hlc": to_clock(t + timedelta(minutes=1)),
        "applied": False,
    }
    assert (unstamped.status_code, unstamped.json()["error"]) == (422, "hlc_required")


# --- Snapshot and feed --------------------------------------------------------------


async def test_the_snapshot_is_protocol_2_without_the_header(client, t) -> None:
    session = await login(client)
    old = datetime.now(UTC) - timedelta(days=40)
    await upload(client, bearer(session), {"courses": [course("CS1")]})
    gone = await client.delete(
        f"/v4/sync/courses/{key('CS2')}", headers=bearer(session), params={"hlc": to_clock(old)}
    )
    assert gone.json()["applied"] is True
    await client.patch(
        f"/v4/sync/courses/{key('CS1')}/override",
        headers=bearer(session),
        json={"color_hex": "#123456", "hlc": to_clock(t)},
    )

    v4 = (await client.get("/v4/sync/full", headers=bearer(session))).json()
    v3 = (await client.get("/v3/sync/full", headers=bearer(session))).json()

    assert v4["course_tombstones"] == [
        {
            "course_key": key("CS2"),
            "course_no": "CS2",
            "semester": SEMESTER,
            "deleted_at": old.isoformat(),
            "hlc": to_clock(old),
            "kind": "hide",
        }
    ]
    assert v3["course_tombstones"] == []
    assert {"server_hlc", "term_resets", "courses_reset_hlc", "roster_requests"} <= v4.keys()
    (override,) = v4["course_overrides"]
    assert override == {**v3["course_overrides"][0], "course_key": key("CS1")}
    assert "course_key" not in v3["course_overrides"][0]


async def test_an_expired_revision_is_a_409_on_v4_and_a_410_on_v3(client) -> None:
    session = await login(client)
    user_id = uuid.UUID(session["user"]["id"])
    await seed_changes(client, session["user"]["id"], 2)
    async with build_session_factory(client.app.state.engine)() as db:
        state = await db.get(UserSyncState, user_id)
        state.compacted_revision = state.current_revision
        await db.commit()
        compacted = state.compacted_revision

    v4 = await client.get("/v4/sync?since_revision=0", headers=bearer(session))
    v3 = await client.get("/v3/sync?since_revision=0", headers=bearer(session))

    fields = {
        "min_available_revision": compacted,
        "current_revision": compacted,
        "full_sync_required": True,
    }
    assert (v3.status_code, v3.json()) == (410, {"error": "sync_revision_expired", **fields})
    assert (v4.status_code, v4.json()) == (
        409,
        {"error": "sync_revision_expired", "message": CODES["sync_revision_expired"], **fields},
    )
    caught_up = await client.get(f"/v4/sync?since_revision={compacted}", headers=bearer(session))
    assert caught_up.json()["changes"] == []
