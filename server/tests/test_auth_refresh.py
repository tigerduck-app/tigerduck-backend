"""End-to-end tests for POST /v3/auth/refresh rotation semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import AuthSession
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
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


async def test_normal_rotation_issues_new_tokens(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] != login["refresh_token"]
    assert body["expires_in"] == 900

    # The new refresh token must itself rotate fine.
    second = await client.post(
        "/v3/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert second.status_code == 200


async def test_unknown_refresh_token_401(client) -> None:
    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": "garbage"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_refresh_token"


async def test_retry_within_grace_returns_fresh_tokens(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert first.status_code == 200
    token_b = first.json()["refresh_token"]

    # Simulated lost response: client retries with A within the grace
    # window — gets fresh tokens, no logout.
    retry = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert retry.status_code == 200
    token_c = retry.json()["refresh_token"]
    assert token_c != token_b

    # C (the retry's token) works.
    ok = await client.post("/v3/auth/refresh", json={"refresh_token": token_c})
    assert ok.status_code == 200
    token_d = ok.json()["refresh_token"]

    # If B surfaces later, the "lost" response was actually intercepted —
    # that's theft: B is rejected AND the device's sessions are revoked.
    stale = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert stale.status_code == 401

    dead = await client.post("/v3/auth/refresh", json={"refresh_token": token_d})
    assert dead.status_code == 401


async def test_reuse_outside_grace_revokes_family(client) -> None:
    client.app.state.settings.auth_refresh_reuse_grace_seconds = 0
    try:
        login = await do_login(client)
        token_a = login["refresh_token"]

        first = await client.post(
            "/v3/auth/refresh", json={"refresh_token": token_a}
        )
        assert first.status_code == 200
        token_b = first.json()["refresh_token"]

        # Reuse of A with zero grace = theft: 401 + the whole family dies.
        reuse = await client.post(
            "/v3/auth/refresh", json={"refresh_token": token_a}
        )
        assert reuse.status_code == 401
        assert reuse.json()["detail"] == "refresh_reuse_detected"

        dead = await client.post(
            "/v3/auth/refresh", json={"refresh_token": token_b}
        )
        assert dead.status_code == 401

        factory = build_session_factory(client.app.state.engine)
        async with factory() as session:
            rows = (await session.execute(select(AuthSession))).scalars().all()
            assert all(r.revoked_at is not None for r in rows)
            assert any(r.reuse_detected_at is not None for r in rows)
    finally:
        client.app.state.settings.auth_refresh_reuse_grace_seconds = 60


async def test_grace_does_not_apply_if_successor_already_used(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    token_b = first.json()["refresh_token"]
    second = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert second.status_code == 200

    # B was redeemed, so the client DID receive the rotation response —
    # a later replay of A is theft even inside the grace window.
    reuse = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"


async def test_expired_session_401(client) -> None:
    login = await do_login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.revoked_at.is_(None))
            )
        ).scalar_one()
        row.issued_at = datetime.now(UTC) - timedelta(days=91)
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "refresh_token_expired"

async def test_grace_retry_is_one_shot(client) -> None:
    """Replaying token A a SECOND time within the grace window must be
    treated as theft, not another benign retry — otherwise an attacker
    holding A can mint unbounded live sessions for 60s."""
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert first.status_code == 200

    retry = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert retry.status_code == 200  # the one benign grace retry
    token_c = retry.json()["refresh_token"]

    second_retry = await client.post(
        "/v3/auth/refresh", json={"refresh_token": token_a}
    )
    assert second_retry.status_code == 401
    assert second_retry.json()["detail"] == "refresh_reuse_detected"

    # Family revoked: the session minted by the first grace retry dies too.
    dead = await client.post("/v3/auth/refresh", json={"refresh_token": token_c})
    assert dead.status_code == 401
