"""The portal's device pages name the people behind devices: names in the
device and list member rows, and the Deregister page's lookup and preview.
In the server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.deregister import (  # noqa: E402
    DeregisterRequest,
    deregister_student,
    lookup_students,
    preview_student,
)
from portal.app.routes.device_lists import list_members  # noqa: E402
from portal.app.routes.devices import list_devices  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _request(path: str = "/api/x") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(actor="ops@example.com"), url=SimpleNamespace(path=path)
    )


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session):
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


async def _person(pool, student_id: str, name: str | None, *devices: tuple[str, str]):
    """A user with one device per (platform, device_class) pair."""
    user_id = await pool.fetchval(
        "INSERT INTO users (student_id, display_name) VALUES ($1, $2) RETURNING id",
        student_id,
        name,
    )
    ids = []
    for i, (platform, device_class) in enumerate(devices):
        ids.append(
            await pool.fetchval(
                "INSERT INTO user_devices (user_id, client_device_id, platform,"
                " device_class, last_seen_at)"
                " VALUES ($1, $2, $3, $4, now() - make_interval(days => $5))"
                " RETURNING id",
                user_id,
                f"{student_id}-{i}",
                platform,
                device_class,
                i,
            )
        )
    return user_id, ids


def _body(response) -> dict:
    return json.loads(response.body)


async def test_device_rows_carry_the_name_and_the_stored_class(pool):
    await _person(pool, "B99900001", "王小明", ("android", "android_tablet"))
    await _person(pool, "B99900002", None, ("ios", ""))

    rows = _body(
        await list_devices(_request("/api/devices"), pool=pool, limit=50, offset=0, search=None)
    )["items"]

    by_student = {r["student_id"]: r for r in rows}
    assert by_student["B99900001"]["display_name"] == "王小明"
    assert by_student["B99900001"]["device_class"] == "android_tablet"
    assert by_student["B99900001"]["last_seen_at"] is not None
    # A device registered before the column existed falls back to its platform.
    assert by_student["B99900002"]["display_name"] is None
    assert by_student["B99900002"]["device_class"] == "iphone"


async def test_the_device_search_matches_a_name(pool):
    await _person(pool, "B99900001", "王小明", ("ios", "iphone"))
    await _person(pool, "B99900002", "陳小華", ("ios", "iphone"))

    rows = _body(
        await list_devices(_request("/api/devices"), pool=pool, limit=50, offset=0, search="小華")
    )["items"]

    assert [r["student_id"] for r in rows] == ["B99900002"]


async def test_list_members_carry_the_name_and_the_stored_class(pool):
    _, (device,) = await _person(pool, "B99900001", "王小明", ("android", "android"))
    list_id = await pool.fetchval(
        "INSERT INTO device_lists (name) VALUES ('beta') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO device_list_members (list_id, device_id) VALUES ($1, $2)",
        list_id,
        device,
    )

    (member,) = _body(
        await list_members(list_id, _request(), pool=pool, limit=50, offset=0)
    )["items"]

    assert (member["student_id"], member["display_name"], member["device_class"]) == (
        "B99900001",
        "王小明",
        "android",
    )


async def test_renaming_a_list_to_a_taken_name_is_a_conflict(pool):
    from portal.app.routes.device_lists import update_list

    await pool.execute("INSERT INTO device_lists (name) VALUES ('beta'), ('staff')")
    staff = await pool.fetchval("SELECT id FROM device_lists WHERE name = 'staff'")

    class _Body(SimpleNamespace):
        async def json(self):
            return {"name": "beta"}

    request = _Body(state=SimpleNamespace(actor="ops@example.com"))
    response = await update_list(staff, request, pool=pool)

    assert response.status_code == 409
    assert await pool.fetchval("SELECT name FROM device_lists WHERE id = $1", staff) == "staff"


async def test_a_lookup_by_student_id_ignores_case_and_returns_that_student(pool):
    await _person(pool, "B99900001", "王小明", ("ios", "iphone"), ("ipados", "ipad"))
    await _person(pool, "B99900011", "王小明", ("ios", "iphone"))

    students = _body(await lookup_students(_request(), pool=pool, q=" b99900001 "))["students"]

    assert len(students) == 1
    assert students[0]["student_id"] == "B99900001"
    assert students[0]["display_name"] == "王小明"
    assert students[0]["devices"] == 2
    assert students[0]["last_active"] is not None
    assert students[0]["joined"] is not None


async def test_a_lookup_by_name_returns_everyone_whose_name_contains_it(pool):
    await _person(pool, "B99900001", "王小明", ("ios", "iphone"))
    await _person(pool, "M99900002", "王小明", ("android", "android"))
    await _person(pool, "B99900003", "林小美", ("ios", "iphone"))

    students = _body(await lookup_students(_request(), pool=pool, q="小明"))["students"]

    assert sorted(s["student_id"] for s in students) == ["B99900001", "M99900002"]


async def test_a_lookup_without_a_match_is_empty(pool):
    await _person(pool, "B99900001", "王小明", ("ios", "iphone"))

    assert _body(await lookup_students(_request(), pool=pool, q="B99900009"))["students"] == []


async def test_the_preview_lists_what_the_deregister_then_deletes(pool):
    user_id, (phone, _) = await _person(
        pool, "B99900001", "王小明", ("ios", "iphone"), ("ipados", "ipad")
    )
    await pool.execute(
        "INSERT INTO auth_sessions (user_id, device_id, refresh_token_hash, expires_at)"
        " VALUES ($1, $2, 'h', now() + interval '1 day')",
        user_id,
        phone,
    )

    preview = _body(await preview_student(_request(), pool=pool, student_id="B99900001"))
    report = _body(
        await deregister_student(
            DeregisterRequest(student_id="B99900001"), _request(), pool=pool
        )
    )

    assert preview["display_name"] == "王小明"
    assert preview["deleted_counts"]["auth_sessions"] == 1
    assert preview["deleted_counts"] == report["deleted_counts"]
    assert preview["devices"] == report["devices"]
    assert await pool.fetchval("SELECT count(*) FROM users") == 0


async def test_a_preview_of_an_unknown_student_is_not_found(pool):
    response = await preview_student(_request(), pool=pool, student_id="B99900009")

    assert response.status_code == 404


async def test_lookup_and_preview_are_audited_without_the_student_id(pool):
    user_id, _ = await _person(pool, "B99900001", "王小明", ("ios", "iphone"))

    await lookup_students(_request("/api/deregister/lookup"), pool=pool, q="小明")
    await preview_student(_request(), pool=pool, student_id="B99900001")

    rows = await pool.fetch(
        "SELECT action, target_user_id, detail::text AS detail FROM admin_audit_log ORDER BY id"
    )
    assert [(r["action"], r["target_user_id"]) for r in rows] == [
        ("user.list", None),
        ("user.view", user_id),
    ]
    assert all(
        "B99900001" not in (r["detail"] or "") and "小明" not in (r["detail"] or "")
        for r in rows
    )
