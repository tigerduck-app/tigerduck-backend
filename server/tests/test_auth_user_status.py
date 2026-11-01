"""A user who is not active loses an existing session at once, as a
revoked session does, on every authenticated route and on refresh."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from server.auth.models import User
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-status", "platform": "ios"},
}
REVOKED = {"detail": "session_revoked"}


async def _login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    return response.json()


async def _set_status(client, user_id: str, status: str) -> None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(
            update(User).where(User.id == uuid.UUID(user_id)).values(status=status)
        )
        await session.commit()


@pytest.mark.parametrize("status", ["suspended", "pending_deletion"])
async def test_a_user_who_is_not_active_gets_the_revoked_answer(client, status) -> None:
    tokens = await _login(client)
    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/v3/sync/revision", headers=bearer)).status_code == 200

    await _set_status(client, tokens["user"]["id"], status)

    protected = await client.get("/v3/sync/revision", headers=bearer)
    refreshed = await client.post(
        "/v3/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert (protected.status_code, protected.json()) == (401, REVOKED)
    assert protected.headers["www-authenticate"] == "Bearer"
    assert (refreshed.status_code, refreshed.json()) == (401, REVOKED)


async def test_an_active_user_keeps_the_session(client) -> None:
    tokens = await _login(client)
    await _set_status(client, tokens["user"]["id"], "active")

    refreshed = await client.post(
        "/v3/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    bearer = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await client.get("/v3/sync/revision", headers=bearer)).status_code == 200
