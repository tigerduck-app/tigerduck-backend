"""End-to-end tests for PATCH /v3/auth/credentials."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text, update

from server.auth.models import ExternalAccount, ExternalAccountCredential, User
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


def allow_moodle(client, fullname: str | None = None) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000", fullname=fullname)
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


class _PoolWatchingVerifier:
    """Accepts every token, noting how many pooled connections are checked
    out while it answers."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.checked_out: int | None = None

    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult:
        self.checked_out = self.engine.pool.checkedout()
        return MoodleVerifyResult(ok=True, username=student_id.lower())


async def test_waiting_on_moodle_holds_no_connection(client) -> None:
    login = await _login(client)
    verifier = _PoolWatchingVerifier(client.app.state.engine)
    client.app.state.moodle_verifier = verifier

    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "fresh-tok"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["updated"] is True
    assert verifier.checked_out == 0


class _AccountDeletingVerifier:
    """Accepts every token, deleting the account while it answers."""

    def __init__(self, engine) -> None:
        self.engine = engine

    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult:
        async with self.engine.begin() as conn:
            await conn.execute(delete(ExternalAccount))
        return MoodleVerifyResult(ok=True, username=student_id.lower())


async def test_an_account_deleted_while_moodle_answers_is_not_updated(client) -> None:
    login = await _login(client)
    client.app.state.moodle_verifier = _AccountDeletingVerifier(client.app.state.engine)

    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "fresh-tok"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["updated"] is False


async def _refresh_with_name(client, login: dict, fullname: str | None) -> None:
    allow_moodle(client, fullname=fullname)
    response = await client.patch(
        "/v3/auth/credentials",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"moodle_token": "fresh-tok"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] is True


async def _stored_name_and_row_version(client) -> tuple[str | None, int]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(User.display_name, text("users.xmin::text::bigint"))
            )
        ).one()
        return row[0], row[1]


async def test_a_credential_refresh_stores_the_moodle_full_name(client) -> None:
    login = await _login(client)

    await _refresh_with_name(client, login, "  Test Student  ")

    name, _ = await _stored_name_and_row_version(client)
    assert name == "Test Student"


@pytest.mark.parametrize("fullname", [None, "", "   "])
async def test_a_refresh_without_a_full_name_keeps_the_stored_one(
    client, fullname
) -> None:
    login = await _login(client)
    await _refresh_with_name(client, login, "Test Student")

    await _refresh_with_name(client, login, fullname)

    name, _ = await _stored_name_and_row_version(client)
    assert name == "Test Student"


async def test_a_refresh_cuts_a_long_full_name_to_the_column(client) -> None:
    login = await _login(client)

    await _refresh_with_name(client, login, "名" * 200)

    name, _ = await _stored_name_and_row_version(client)
    assert name == "名" * 128


async def test_a_refresh_with_the_same_name_leaves_the_row_alone(client) -> None:
    login = await _login(client)
    await _refresh_with_name(client, login, "Test Student")
    _, version = await _stored_name_and_row_version(client)

    await _refresh_with_name(client, login, " Test Student ")

    assert await _stored_name_and_row_version(client) == ("Test Student", version)
