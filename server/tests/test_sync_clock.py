"""The TigerSync clock on the wire: the header every /v3 answer carries, and
the edits stamped on arrival less the `age_ms` their device sends."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import build_session_factory, get_session, session_scope
from server.routes.sync.snapshot import _read_full_snapshot
from server.sync.merge import CLOCK_HEADER, MAX_EDIT_AGE, to_clock
from server.tests.test_api_v4 import login
from server.tests.test_holiday_overrides import seed_holiday
from server.tests.test_sync_overrides import bearer, do_login, seed_assignment

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _clock(response) -> int:
    return int(response.headers[CLOCK_HEADER])


async def test_every_v3_answer_carries_the_server_clock(client) -> None:
    login = await do_login(client)
    before = to_clock(datetime.now(UTC))

    ok = await client.get("/v3/sync/revision", headers=bearer(login))
    missing = await client.patch(
        "/v3/sync/courses/1141CS3001301/override",
        headers=bearer(login),
        json={"color_hex": "#FF8800"},
    )
    invalid = await client.patch(
        "/v3/sync/assignments/9001/override",
        headers=bearer(login),
        json={"local_status": "done"},
    )

    assert (ok.status_code, missing.status_code, invalid.status_code) == (200, 404, 422)
    after = to_clock(datetime.now(UTC))
    for response in (ok, missing, invalid):
        assert before <= _clock(response) <= after


async def test_answers_outside_v3_carry_no_clock(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert CLOCK_HEADER not in response.headers


async def test_a_snapshot_carries_the_server_clock(client) -> None:
    login = await do_login(client)
    before = to_clock(datetime.now(UTC))

    response = await client.get("/v3/sync/full", headers=bearer(login))

    assert response.status_code == 200
    assert before <= response.json()["server_hlc"] <= _clock(response)


async def test_a_snapshot_reads_the_server_clock_before_its_data(client) -> None:
    """A snapshot whose clock is later than a write's header includes that
    write only if the clock is read before the query that fixes the data."""
    login = await do_login(client)
    write = await client.get("/v3/sync/revision", headers=bearer(login))
    first_query: list[int] = []

    def record(*_args) -> None:
        if not first_query:
            first_query.append(to_clock(datetime.now(UTC)))

    sync_engine = client.app.state.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        async with build_session_factory(client.app.state.engine)() as session:
            snapshot = await _read_full_snapshot(
                session, uuid.UUID(login["user"]["id"]), None
            )
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)

    assert _clock(write) < snapshot["server_hlc"] <= first_query[0]


async def test_an_unhandled_error_carries_the_server_clock(client) -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    client.app.add_api_route("/v3/test-failure", fail)
    transport = ASGITransport(app=client.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as unraised:
        before = to_clock(datetime.now(UTC))
        response = await unraised.get("/v3/test-failure")

    assert response.status_code == 500
    assert before <= _clock(response) <= to_clock(datetime.now(UTC))


# --- age_ms -----------------------------------------------------------------------

AGE_MS = 3_600_000
KEY = "client:1141:CS1"
COURSE = {"semester": "1141", "course_no": "CS1", "course_name": "CS1"}


async def timed(client, method: str, path: str, headers: dict, **request) -> tuple[int, dict, int]:
    before = to_clock(datetime.now(UTC))
    response = await client.request(method, path, headers=headers, **request)
    assert response.status_code == 200, (path, response.text)
    return before, response.json(), to_clock(datetime.now(UTC))


async def age_writes(client, session: dict) -> list[tuple]:
    """Every /v4 write that takes `age_ms`, with where its answer puts the stamp."""
    await seed_assignment(client, session["user"]["id"], moodle_course_id=101)
    holiday_id = await seed_holiday(client)
    age = {"age_ms": AGE_MS}
    return [
        (
            "POST", "/v4/sync/courses/upload",
            {"json": {"courses": [COURSE], "adds": [{"course_key": KEY, **age}]}},
            lambda answer: answer["adds"][0]["hlc"],
        ),
        (
            "PATCH", f"/v4/sync/courses/{KEY}/override",
            {"json": {"color_hex": "#FF8800", **age}},
            lambda answer: answer["color_hex_hlc"],
        ),
        (
            "PATCH", "/v4/sync/assignments/9001/override",
            {"json": {"local_status": "ignored", **age}},
            lambda answer: answer["local_status_hlc"],
        ),
        (
            "PUT", f"/v4/sync/holiday-overrides/{holiday_id}",
            {"json": {"notify": True, **age}},
            lambda answer: answer["hlc"],
        ),
        (
            "DELETE", "/v4/sync/courses/client:1141:CS2",
            {"params": age},
            lambda answer: answer["hlc"],
        ),
        (
            "DELETE", "/v4/sync/courses",
            {"params": {"semester": "1142", **age}},
            lambda answer: answer["reset_hlc"],
        ),
    ]


async def test_every_write_is_stamped_its_age_before_it_arrived(client) -> None:
    session = await login(client)
    for method, path, request, stamp_of in await age_writes(client, session):
        before, answer, after = await timed(client, method, path, bearer(session), **request)
        assert before - AGE_MS * 1000 <= stamp_of(answer) <= after - AGE_MS * 1000, path


async def test_a_write_held_up_before_its_handler_is_stamped_from_its_arrival(client) -> None:
    """Authentication and the handler wait on the database, under load for
    longer; an edit that waited must not count as made later."""
    session = await login(client)
    writes = await age_writes(client, session)
    wait = timedelta(milliseconds=500)

    async def opens_late(request: Request) -> AsyncIterator[AsyncSession]:
        await asyncio.sleep(wait.total_seconds())
        async with session_scope(request.app.state.session_factory) as db:
            yield db

    client.app.dependency_overrides[get_session] = opens_late
    try:
        for method, path, request, stamp_of in writes:
            before, answer, _ = await timed(client, method, path, bearer(session), **request)
            arrival = stamp_of(answer) + AGE_MS * 1000
            assert before <= arrival < before + (wait / 2) // timedelta(microseconds=1), path
    finally:
        del client.app.dependency_overrides[get_session]


async def test_protocol_2_on_v3_takes_an_age_too(client) -> None:
    session = await do_login(client)
    headers = {**bearer(session), "X-TigerSync-Protocol": "2"}
    for method, path, request, stamp_of in (
        (
            "POST", "/v3/sync/courses/upload",
            {"json": {"courses": [COURSE], "adds": [{"course_key": KEY, "age_ms": AGE_MS}]}},
            lambda answer: answer["adds"][0]["hlc"],
        ),
        (
            "DELETE", "/v3/sync/courses/client:1141:CS2",
            {"params": {"age_ms": AGE_MS}},
            lambda answer: answer["hlc"],
        ),
    ):
        before, answer, after = await timed(client, method, path, headers, **request)
        assert before - AGE_MS * 1000 <= stamp_of(answer) <= after - AGE_MS * 1000, path


async def test_an_age_is_clamped_to_between_arrival_and_a_year_before(client) -> None:
    session = await login(client)
    year_us = MAX_EDIT_AGE // timedelta(microseconds=1)

    before, negative, after = await timed(
        client, "DELETE", "/v4/sync/courses/client:1141:CS1", bearer(session),
        params={"age_ms": -5_000},
    )
    assert before <= negative["hlc"] <= after

    before, huge, after = await timed(
        client, "DELETE", "/v4/sync/courses/client:1141:CS2", bearer(session),
        params={"age_ms": 2**63 - 1},
    )
    assert before - year_us <= huge["hlc"] <= after - year_us


async def test_an_age_wins_over_an_hlc_and_is_never_clock_ahead(client) -> None:
    session = await login(client)
    now = datetime.now(UTC)
    for course_no, hlc in (
        ("CS1", to_clock(now + timedelta(hours=1))),
        ("CS2", to_clock(now - timedelta(days=7))),
    ):
        before, answer, after = await timed(
            client, "DELETE", f"/v4/sync/courses/client:1141:{course_no}", bearer(session),
            params={"hlc": hlc, "age_ms": 1_000},
        )
        assert before - 1_000_000 <= answer["hlc"] <= after - 1_000_000, course_no


async def test_a_retry_with_its_grown_age_lands_on_about_the_first_stamp(client) -> None:
    session = await login(client)
    path = "/v4/sync/courses/client:1141:CS1"

    sent = time.monotonic()
    before, first, after = await timed(
        client, "DELETE", path, bearer(session), params={"age_ms": 60_000}
    )
    retry_age = 60_000 + round((time.monotonic() - sent) * 1000)
    _, retry, _ = await timed(client, "DELETE", path, bearer(session), params={"age_ms": retry_age})

    assert before - 60_000_000 <= first["hlc"] <= after - 60_000_000
    assert abs(retry["hlc"] - first["hlc"]) < 1_000_000


async def test_without_an_age_an_hlc_and_an_arrival_stamp_stand_as_before(client) -> None:
    session = await login(client)
    holiday_id = await seed_holiday(client)
    path = f"/sync/holiday-overrides/{holiday_id}"
    stamp = to_clock(datetime.now(UTC) - timedelta(hours=2))

    _, stamped, _ = await timed(
        client, "PUT", f"/v4{path}", bearer(session), json={"notify": True, "hlc": stamp}
    )
    assert stamped["hlc"] == stamp
    before, released, after = await timed(
        client, "PUT", f"/v3{path}", bearer(session), json={"notify": False}
    )
    assert before <= released["hlc"] <= after
