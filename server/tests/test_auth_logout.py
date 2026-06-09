"""Tests for the Bearer dependency and POST /v3/auth/logout."""

from __future__ import annotations

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier

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


async def test_logout_revokes_session(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/auth/logout",
        headers={"Authorization": f"Bearer {login['access_token']}"},
    )
    assert response.status_code == 204

    # The session's refresh token must be dead now.
    refresh = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert refresh.status_code == 401


async def test_logout_is_idempotent(client) -> None:
    login = await do_login(client)
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    first = await client.post("/v3/auth/logout", headers=headers)
    second = await client.post("/v3/auth/logout", headers=headers)
    assert first.status_code == 204
    assert second.status_code == 204


async def test_missing_bearer_401(client) -> None:
    response = await client.post("/v3/auth/logout")
    assert response.status_code == 401


async def test_garbage_bearer_401(client) -> None:
    response = await client.post(
        "/v3/auth/logout", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


async def test_non_bearer_scheme_401(client) -> None:
    response = await client.post(
        "/v3/auth/logout", headers={"Authorization": "Basic dXNlcjpwdw=="}
    )
    assert response.status_code == 401
