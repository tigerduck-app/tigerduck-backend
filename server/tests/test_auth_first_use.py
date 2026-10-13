"""auth_sessions.first_used_at: set by a session's first authenticated
request or by its rotation, and written once."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine

from server.auth.models import AuthSession
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.auth.tokens import hash_refresh_token
from server.db import build_session_factory

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


def _bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _row(client, refresh_token: str) -> AuthSession:
    token_hash = hash_refresh_token(
        client.app.state.settings.auth_refresh_hmac_key, refresh_token
    )
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(AuthSession).where(AuthSession.refresh_token_hash == token_hash)
            )
        ).scalar_one()


@contextmanager
def _session_writes(engine: AsyncEngine) -> Iterator[list[str]]:
    """The UPDATEs of auth_sessions sent while the block runs."""
    writes: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("UPDATE AUTH_SESSIONS"):
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield writes
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_the_first_request_writes_first_used_at_and_later_ones_do_not(
    client,
) -> None:
    login = await do_login(client)
    assert (await _row(client, login["refresh_token"])).first_used_at is None

    with _session_writes(client.app.state.engine) as first_writes:
        first = await client.get("/v3/devices", headers=_bearer(login))
    first_used_at = (await _row(client, login["refresh_token"])).first_used_at
    with _session_writes(client.app.state.engine) as later_writes:
        for _ in range(3):
            later = await client.get("/v3/devices", headers=_bearer(login))
            assert later.status_code == 200

    assert first.status_code == 200
    assert len(first_writes) == 1
    assert first_used_at is not None
    assert later_writes == []
    assert (await _row(client, login["refresh_token"])).first_used_at == first_used_at


async def test_first_use_outlives_a_request_that_fails(client) -> None:
    """The client showed it holds the session even when the route then
    answers an error and rolls back its own work."""
    login = await do_login(client)

    missing = await client.delete("/v3/devices/not-a-device", headers=_bearer(login))

    assert missing.status_code == 404
    assert (await _row(client, login["refresh_token"])).first_used_at is not None


async def test_rotation_counts_as_use_of_the_old_session(client) -> None:
    login = await do_login(client)

    rotated = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )

    assert rotated.status_code == 200
    assert (await _row(client, login["refresh_token"])).first_used_at is not None
    assert (await _row(client, rotated.json()["refresh_token"])).first_used_at is None
