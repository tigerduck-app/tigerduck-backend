"""The TigerSync clock on the wire: the header every /v3 answer carries."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from server.db import build_session_factory
from server.routes.sync.snapshot import _read_full_snapshot
from server.sync.merge import CLOCK_HEADER, to_clock
from server.tests.test_sync_overrides import bearer, do_login

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
