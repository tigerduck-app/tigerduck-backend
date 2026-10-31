"""One account on both versions (docs/api-v4.md, section 3): a write on one
version is read on the other, and a `/v4` write logs the same changes and
queues the same `sync_trigger` as its `/v3` twin."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import PushJob
from server.db import build_session_factory
from server.sync.merge import to_clock
from server.sync.models import UserChangeLog
from server.tests.test_api_v4 import bearer, login
from server.tests.test_holiday_overrides import seed_holiday
from server.tests.test_sync_overrides import seed_assignment

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEMESTER = "1141"
PROTOCOL = {"X-TigerSync-Protocol": "2"}


def key(course_no: str) -> str:
    return f"client:{SEMESTER}:{course_no}"


def course(course_no: str) -> dict:
    return {"semester": SEMESTER, "course_no": course_no, "course_name": course_no}


async def write_everything(client, version: str, headers: dict, holiday_id: int, t: datetime):
    """Every TigerSync write the apps make, in one order, as `version`
    sends it. Answers each write's reply."""

    def at(minutes: int) -> int:
        return to_clock(t + timedelta(minutes=minutes))

    def course_override(course_no: str) -> str:
        ref = key(course_no) if version == "v4" else f"{SEMESTER}{course_no}"
        return f"/{version}/sync/courses/{ref}/override"

    writes = [
        ("DELETE", "/sync/courses", {"params": {"semester": SEMESTER, "hlc": at(0)}}),
        ("POST", "/sync/courses/upload", {"json": {
            "courses": [course("CS1"), course("CS2"), course("CS3")],
            "adds": [{"course_key": key("CS3"), "hlc": at(1)}],
        }}),
        ("PATCH", course_override("CS1"), {"json": {"color_hex": "#FF8800", "hlc": at(2)}}),
        ("PATCH", course_override("CS1"), {"json": {
            "custom_name": "Algorithms", "locale": "en", "hlc": at(3),
        }}),
        ("PATCH", "/sync/assignments/9001/override", {"json": {
            "local_status": "locally_completed", "hlc": at(4),
        }}),
        ("PATCH", "/sync/assignments/9001/override", {"json": {"local_status": "none", "hlc": at(5)}}),
        ("PUT", f"/sync/holiday-overrides/{holiday_id}", {"json": {"notify": True, "hlc": at(6)}}),
        ("DELETE", f"/sync/courses/{key('CS2')}", {"params": {"hlc": at(7), "kind": "hide"}}),
        ("DELETE", f"/sync/courses/{key('CS3')}", {"params": {"hlc": at(8), "kind": "remove"}}),
    ]
    replies = []
    for method, path, body in writes:
        url = path if path.startswith("/v") else f"/{version}{path}"
        response = await client.request(method, url, headers=headers, **body)
        assert response.status_code == 200, (url, response.text)
        replies.append(response.json())
    return replies


async def changes(client, user_id: str) -> list[tuple]:
    async with build_session_factory(client.app.state.engine)() as db:
        rows = await db.execute(
            select(UserChangeLog.entity_type, UserChangeLog.operation, UserChangeLog.payload)
            .where(UserChangeLog.user_id == uuid.UUID(user_id))
            .order_by(UserChangeLog.revision)
        )
        return [tuple(row) for row in rows]


async def sync_triggers(client, user_id: str) -> list[tuple]:
    async with build_session_factory(client.app.state.engine)() as db:
        jobs = await db.execute(
            select(PushJob.channel, PushJob.status, PushJob.payload).where(
                PushJob.user_id == uuid.UUID(user_id)
            )
        )
        return [(channel, state, payload["kind"]) for channel, state, payload in jobs]


def digest(snapshot: dict) -> dict:
    """A snapshot without what differs between two accounts: row ids and
    the times of day the rows were written."""
    return {
        "courses": sorted(
            (c["course_key"], c["source"], c["added_hlc"], c["enrollment_status"])
            for c in snapshot["courses"]
        ),
        "tombstones": sorted(
            (t["course_key"], t["kind"], t["hlc"]) for t in snapshot["course_tombstones"]
        ),
        "term_resets": snapshot["term_resets"],
        "courses_reset_hlc": snapshot["courses_reset_hlc"],
        "overrides": sorted(
            (o["course_no"], o["color_hex"], o["color_hex_hlc"], str(o["custom_names"]),
             str(o["custom_names_hlc"]))
            for o in snapshot["course_overrides"]
        ),
        "holidays": snapshot["holiday_overrides"],
        "marks": sorted(
            (a["moodle_assignment_id"], a["local_status"], a["local_status_hlc"])
            for a in snapshot["assignment_overrides"]
        ),
    }


def without(reply: dict, *fields: str) -> dict:
    return {k: v for k, v in reply.items() if k not in fields}


async def test_a_v4_write_logs_and_pushes_as_its_v3_twin(client) -> None:
    t = datetime.now(UTC) - timedelta(hours=2)
    holiday_id = await seed_holiday(client)
    old = await login(client, prefix="/v3", student_id="B11015000")
    new = await login(client, prefix="/v4", student_id="B11015001")
    for session in (old, new):
        await seed_assignment(client, session["user"]["id"], moodle_course_id=101)

    v3 = await write_everything(client, "v3", {**bearer(old), **PROTOCOL}, holiday_id, t)
    v4 = await write_everything(client, "v4", bearer(new), holiday_id, t)

    logged = await changes(client, new["user"]["id"])
    assert logged and logged == await changes(client, old["user"]["id"])
    assert await sync_triggers(client, new["user"]["id"]) == await sync_triggers(
        client, old["user"]["id"]
    )
    assert await sync_triggers(client, new["user"]["id"]) == [("system", "pending", "sync_trigger")]

    # The replies differ only where docs/api-v4.md says they do.
    reset, upload, colour, name, mark, clear, holiday, hide, remove = range(9)
    for i in (reset, holiday, hide, remove):
        assert v4[i] == v3[i]
    assert v4[upload] == without(v3[upload], "overrides_applied")
    for i in (colour, name):
        assert without(v4[i], "id", "updated_at") == without(v3[i], "id", "updated_at")
    for i in (mark, clear):
        assert v4[i] == without(v3[i], "id", "updated_at", "deleted")

    # Each account reads the same on both versions.
    for session in (old, new):
        feed = [
            (await client.get(f"/{v}/sync?since_revision=0", headers=bearer(session))).json()
            for v in ("v3", "v4")
        ]
        assert feed[0] == feed[1]
    snapshots = {
        (version, who): (
            await client.get(f"/{version}/sync/full", headers={**bearer(session), **PROTOCOL})
        ).json()
        for version in ("v3", "v4")
        for who, session in (("old", old), ("new", new))
    }
    assert len({str(digest(s)) for s in snapshots.values()}) == 1
    assert digest(snapshots["v4", "new"])["tombstones"] == [
        (key("CS2"), "hide", to_clock(t + timedelta(minutes=7))),
        (key("CS3"), "remove", to_clock(t + timedelta(minutes=8))),
    ]


async def test_tokens_from_either_version_work_on_both(client) -> None:
    session = await login(client, prefix="/v3")
    assert (await client.get("/v4/sync/revision", headers=bearer(session))).status_code == 200

    refreshed = await client.post(
        "/v4/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    for version in ("v3", "v4"):
        answer = await client.get(f"/{version}/sync/revision", headers=bearer(refreshed.json()))
        assert answer.status_code == 200


async def test_a_released_v3_app_and_a_v4_app_read_each_others_writes(client) -> None:
    """A 3.x app sends no protocol header, forces its hand-adds and uploads
    its colours; a v4 app on the same account PATCHes by course key."""
    released = await login(client, prefix="/v3", device="mac")
    current = await login(client, prefix="/v4", device="iphone")
    assert released["user"]["id"] == current["user"]["id"]

    uploaded = await client.post(
        "/v3/sync/courses/upload",
        headers=bearer(released),
        json={
            "courses": [course("CS1"), course("CS2")],
            "force_keys": [key("CS2")],
            "course_overrides": [{"course_key": key("CS1"), "color_hex": "#FF8800"}],
        },
    )
    assert uploaded.json()["overrides_applied"] == 1

    snapshot = (await client.get("/v4/sync/full", headers=bearer(current))).json()
    assert {c["course_key"]: c["source"] for c in snapshot["courses"]} == {
        key("CS1"): "ntust_portal", key("CS2"): "user_added",
    }
    assert [(o["course_key"], o["color_hex"]) for o in snapshot["course_overrides"]] == [
        (key("CS1"), "#FF8800")
    ]

    stamp = to_clock(datetime.now(UTC))
    recoloured = await client.patch(
        f"/v4/sync/courses/{key('CS1')}/override",
        headers=bearer(current),
        json={"color_hex": "#0088FF", "hlc": stamp},
    )
    assert recoloured.json()["applied"] == ["color_hex"]
    removed = await client.delete(
        f"/v4/sync/courses/{key('CS2')}", headers=bearer(current), params={"hlc": stamp}
    )
    assert removed.json()["applied"] is True

    old_snapshot = (await client.get("/v3/sync/full", headers=bearer(released))).json()
    assert [(o["moodle_id"], o["color_hex"]) for o in old_snapshot["course_overrides"]] == [
        (f"{SEMESTER}CS1", "#0088FF")
    ]
    assert [c["course_key"] for c in old_snapshot["courses"]] == [key("CS1")]
    feed = (await client.get("/v3/sync?since_revision=0", headers=bearer(released))).json()
    assert [(c["entity_type"], c["operation"]) for c in feed["changes"]][-2:] == [
        ("course_override", "upsert"),
        ("course", "delete"),
    ]
