"""Every writer of a user's sync data takes the user's sync lock
(`lock_sync_state`) before it writes a row. The override writes take it
before they read, so a writer holding rows while it waits for the lock
would deadlock with them."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import event

from server.tests.test_sync_overrides import bearer, do_login, seed_course

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "client:1141:CS3001301"
SYNC_TABLES = {
    "user_courses",
    "user_course_overrides",
    "user_course_tombstones",
    "user_course_skipped_dates",
    "user_assignments",
    "user_assignment_overrides",
}
_WRITE = re.compile(r"\s*(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)")


async def _statements(client, request) -> list[str]:
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        seen.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await request()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code in (200, 204), response.text
    return seen


def _lock_comes_first(statements: list[str]) -> bool:
    lock = next(
        i for i, s in enumerate(statements) if "FROM user_sync_state" in s and "FOR UPDATE" in s
    )
    write = next(
        i
        for i, s in enumerate(statements)
        if (m := _WRITE.match(s)) and m.group(1) in SYNC_TABLES
    )
    return lock < write


async def _seeded(client) -> tuple[dict, int]:
    login = await do_login(client)
    course_id = await seed_course(
        client, login["user"]["id"], semester="1141", course_key=KEY, moodle_id="1141CS3001301"
    )
    response = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"color_hex": "#FF0000"},
    )
    assert response.status_code == 200
    return login, course_id


@pytest.mark.parametrize(
    "request_for",
    [
        lambda c, h, _id: c.delete(f"/v3/sync/courses/{KEY}", headers=h),
        lambda c, h, _id: c.delete("/v3/sync/courses", headers=h, params={"semester": "1141"}),
        lambda c, h, _id: c.delete("/v3/sync/courses", headers=h),
        lambda c, h, _id: c.post(
            "/v3/sync/courses/upload",
            headers=h,
            json={
                "courses": [
                    {"semester": "1141", "course_no": "CS3001301", "course_name": "OS"}
                ],
                "course_overrides": [{"course_key": KEY, "color_hex": "#00FF00"}],
            },
        ),
        lambda c, h, course_id: c.put(
            f"/v3/courses/{course_id}/skipped-dates/2026-10-26", headers=h, json={}
        ),
        lambda c, h, _id: c.patch(
            "/v3/devices/iphone-abc/preferences", headers=h, json={"sync_courses": False}
        ),
    ],
    ids=["course delete", "term reset", "full reset", "roster upload", "skipped date", "sync off"],
)
async def test_a_writer_takes_the_sync_lock_before_any_row(client, request_for) -> None:
    login, course_id = await _seeded(client)
    statements = await _statements(
        client, lambda: request_for(client, bearer(login), course_id)
    )
    assert _lock_comes_first(statements)


async def test_removing_a_skipped_date_takes_the_sync_lock_first(client) -> None:
    login, course_id = await _seeded(client)
    path = f"/v3/courses/{course_id}/skipped-dates/2026-10-26"
    assert (await client.put(path, headers=bearer(login), json={})).status_code == 200
    statements = await _statements(client, lambda: client.delete(path, headers=bearer(login)))
    assert _lock_comes_first(statements)


async def test_a_preference_update_takes_the_sync_lock_before_the_device_row(client) -> None:
    """A roster upload holds the sync lock while it updates the device's
    `last_seen_at`, so the preference update must lock in the same order."""
    login, _ = await _seeded(client)
    statements = await _statements(
        client,
        lambda: client.patch(
            "/v3/devices/iphone-abc/preferences",
            headers=bearer(login),
            json={"sync_assignments": False},
        ),
    )
    lock = next(
        i for i, s in enumerate(statements) if "FROM user_sync_state" in s and "FOR UPDATE" in s
    )
    device = next(i for i, s in enumerate(statements) if _WRITE.match(s) and "user_devices" in s)
    assert lock < device


_TOUCH = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+(\w+)")


@pytest.mark.parametrize(
    "request_for",
    [
        lambda c, h: c.delete(f"/v3/sync/courses/{KEY}", headers=h, params={"hlc": 1}),
        lambda c, h: c.delete("/v3/sync/courses", headers=h, params={"semester": "1141", "hlc": 1}),
        lambda c, h: c.post(
            "/v3/sync/courses/upload",
            headers=h,
            json={
                "courses": [{"semester": "1141", "course_no": "CS3001301", "course_name": "OS"}],
                "adds": [{"course_key": KEY, "hlc": 1}],
            },
        ),
    ],
    ids=["course delete", "term reset", "roster upload"],
)
async def test_a_protocol_2_writer_reads_its_rows_under_the_sync_lock(client, request_for) -> None:
    """The stamps it compares are only current under the lock."""
    login, _ = await _seeded(client)
    headers = {**bearer(login), "X-TigerSync-Protocol": "2"}
    statements = await _statements(client, lambda: request_for(client, headers))
    lock = next(
        i for i, s in enumerate(statements) if "FROM user_sync_state" in s and "FOR UPDATE" in s
    )
    first = next(
        i
        for i, s in enumerate(statements)
        if any(table in SYNC_TABLES for table in _TOUCH.findall(s))
    )
    assert lock < first
