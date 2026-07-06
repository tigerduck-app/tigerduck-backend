"""End-to-end tests for PATCH /v3/auth/credentials."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from server.auth.models import ExternalAccount, ExternalAccountCredential
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "moodle-tok-1",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


def allow_moodle(client) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )


def deny_moodle(client, error: str = "invalid_token") -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=False, error=error)
    )


async def _login(client) -> dict:
    allow_moodle(client)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    return response.json()


async def _mark_account_invalid_with_disabled_jobs(client) -> None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        account = (
            await session.execute(select(ExternalAccount))
        ).scalar_one()
        account.credential_status = "invalid"
        await session.execute(
            update(SyncJob).values(status="disabled", attempts=3)
        )
        await session.commit()


async def test_rejected_token_does_not_activate_or_revive(client) -> None:
    login = await _login(client)
    await _mark_account_invalid_with_disabled_jobs(client)

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        original_ciphertext = (
            await session.execute(select(ExternalAccountCredential))
        ).scalar_one().ciphertext

    deny_moodle(client)
    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "stale-tok"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] is False

    async with factory() as session:
        account = (
            await session.execute(select(ExternalAccount))
        ).scalar_one()
        assert account.credential_status == "invalid"

        jobs = (await session.execute(select(SyncJob))).scalars().all()
        assert jobs
        for job in jobs:
            assert job.status == "disabled"
            assert job.attempts == 3

        credential = (
            await session.execute(select(ExternalAccountCredential))
        ).scalar_one()
        assert credential.ciphertext == original_ciphertext
        assert credential.rotated_at is None


async def test_unreachable_moodle_does_not_activate_or_revive(client) -> None:
    login = await _login(client)
    await _mark_account_invalid_with_disabled_jobs(client)

    deny_moodle(client, error="unreachable")
    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "maybe-fine-tok"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] is False

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        account = (
            await session.execute(select(ExternalAccount))
        ).scalar_one()
        assert account.credential_status == "invalid"
        jobs = (await session.execute(select(SyncJob))).scalars().all()
        assert all(j.status == "disabled" for j in jobs)


async def test_verified_token_activates_and_revives(client) -> None:
    login = await _login(client)
    await _mark_account_invalid_with_disabled_jobs(client)

    allow_moodle(client)
    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "fresh-tok"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] is True

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        account = (
            await session.execute(select(ExternalAccount))
        ).scalar_one()
        assert account.credential_status == "active"
        assert account.last_auth_success_at is not None

        jobs = (await session.execute(select(SyncJob))).scalars().all()
        assert jobs
        for job in jobs:
            assert job.status == "pending"
            assert job.attempts == 0

        credential = (
            await session.execute(select(ExternalAccountCredential))
        ).scalar_one()
        assert credential.rotated_at is not None


async def test_credentials_rate_limited_per_user(client) -> None:
    """Every call hits Moodle for verification, so the endpoint must throttle
    per user to prevent amplification against NTUST Moodle."""
    from server.routes.auth import _CREDENTIALS_MAX_ATTEMPTS

    login = await _login(client)
    allow_moodle(client)

    headers = {"Authorization": f"Bearer {login['access_token']}"}
    for _ in range(_CREDENTIALS_MAX_ATTEMPTS):
        response = await client.patch(
            "/v3/auth/credentials", headers=headers, json={"moodle_token": "tok"}
        )
        assert response.status_code == 200, response.text

    response = await client.patch(
        "/v3/auth/credentials", headers=headers, json={"moodle_token": "tok"}
    )
    assert response.status_code == 429
