"""End-to-end tests for POST /v3/auth/login."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher, EncryptedBlob
from server.auth.models import (
    AuthSession,
    ExternalAccount,
    ExternalAccountCredential,
    User,
    UserDevice,
)
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "correct horse battery staple",
    "moodle_token": "moodle-tok-1",
    "moodle_private_token": "moodle-priv-1",
    "device_info": {
        "client_device_id": "iphone-abc",
        "platform": "ios",
        "device_name": "iPhone 17",
        "app_version": "3.0.0",
        "os_version": "iOS 19.0",
    },
}


def allow_moodle(client, username: str = "b11015000") -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username=username)
    )


def deny_moodle(client, error: str = "invalid_token") -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=False, error=error)
    )


async def test_login_success_creates_full_identity(client) -> None:
    allow_moodle(client)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["user"]["student_id"] == "B11015000"
    assert body["device_id"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        user = (
            await session.execute(
                select(User).where(User.student_id == "B11015000")
            )
        ).scalar_one()
        account = (
            await session.execute(
                select(ExternalAccount).where(ExternalAccount.user_id == user.id)
            )
        ).scalar_one()
        assert account.provider == "ntust_sso"
        assert account.credential_status == "active"

        cred = await session.get(ExternalAccountCredential, account.id)
        assert cred is not None
        cipher = CredentialCipher.from_settings(client.app.state.settings)
        blob = EncryptedBlob(
            key_id=cred.encryption_key_id,
            nonce=cred.nonce,
            ciphertext=cred.ciphertext,
            aad=cred.aad,
        )
        decrypted = cipher.decrypt(blob)
        assert decrypted["ntust_password"] == LOGIN_BODY["password"]
        # Security review 1.4: backend never verified the password, so it
        # must be stored unverified and never auto-retried against SSO.
        assert decrypted["password_verified"] is False
        assert decrypted["token_cache"]["moodle_token"] == "moodle-tok-1"

        device = (
            await session.execute(
                select(UserDevice).where(UserDevice.user_id == user.id)
            )
        ).scalar_one()
        assert device.client_device_id == "iphone-abc"

        sessions = (
            await session.execute(
                select(AuthSession).where(AuthSession.user_id == user.id)
            )
        ).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None


async def test_android_login_skips_credential_and_sync(client) -> None:
    """Android self-syncs on-device, so the backend must NOT persist its
    NTUST credentials or provision a server-side sync job. The Moodle token
    only authenticates the login here, then is discarded."""
    allow_moodle(client)
    body = {
        **LOGIN_BODY,
        "device_info": {
            **LOGIN_BODY["device_info"],
            "client_device_id": "android-abc",
            "platform": "android",
        },
    }
    response = await client.post("/v3/auth/login", json=body)
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        user = (
            await session.execute(
                select(User).where(User.student_id == "B11015000")
            )
        ).scalar_one()
        account = (
            await session.execute(
                select(ExternalAccount).where(ExternalAccount.user_id == user.id)
            )
        ).scalar_one()
        # No NTUST credential persisted for an Android device...
        assert await session.get(ExternalAccountCredential, account.id) is None
        # ...and the account is not advertised as server-syncable, so a stray
        # /sync-jobs/run-now can't provision a job the worker can't run.
        assert account.credential_status != "active"
        # No server-side sync job provisioned at login.
        jobs = (
            await session.execute(
                select(SyncJob).where(SyncJob.user_id == user.id)
            )
        ).scalars().all()
        assert jobs == []


async def test_second_login_reuses_user_and_device(client) -> None:
    allow_moodle(client)
    first = await client.post("/v3/auth/login", json=LOGIN_BODY)
    second = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert second.status_code == 200
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert first.json()["device_id"] == second.json()["device_id"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        users = (await session.execute(select(User))).scalars().all()
        assert len(users) == 1
        devices = (await session.execute(select(UserDevice))).scalars().all()
        assert len(devices) == 1
        # Two live sessions (one per login) is expected.
        sessions = (await session.execute(select(AuthSession))).scalars().all()
        assert len(sessions) == 2


async def test_moodle_failure_returns_401_and_creates_nothing(client) -> None:
    deny_moodle(client)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 401
    assert response.json()["detail"] == "moodle_verification_failed"

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []


async def test_login_rate_limited_after_max_attempts(client) -> None:
    deny_moodle(client)
    for _ in range(5):
        response = await client.post("/v3/auth/login", json=LOGIN_BODY)
        assert response.status_code == 401
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 429
    assert response.json()["detail"] == "too_many_login_attempts"


async def test_login_reactivates_soft_deleted_user(client) -> None:
    # Pre-seed a soft-deleted user owning the external account (review 1.7).
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        user = User(student_id=None, deleted_at=datetime.now(UTC))
        session.add(user)
        await session.flush()
        session.add(
            ExternalAccount(
                user_id=user.id,
                provider="ntust_sso",
                external_user_id="B11015000",
            )
        )
        await session.commit()
        seeded_user_id = str(user.id)

    allow_moodle(client)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    assert response.json()["user"]["id"] == seeded_user_id

    async with factory() as session:
        revived = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert revived.deleted_at is None
        assert revived.status == "active"
        assert revived.student_id == "B11015000"


async def test_suspended_user_rejected(client) -> None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        user = User(student_id="B11015000", status="suspended")
        session.add(user)
        await session.flush()
        session.add(
            ExternalAccount(
                user_id=user.id,
                provider="ntust_sso",
                external_user_id="B11015000",
            )
        )
        await session.commit()

    allow_moodle(client)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 403
    assert response.json()["detail"] == "account_suspended"


async def test_unconfigured_auth_returns_503(client) -> None:
    client.app.state.settings.auth_jwt_secret = ""
    try:
        allow_moodle(client)
        response = await client.post("/v3/auth/login", json=LOGIN_BODY)
        assert response.status_code == 503
        assert response.json()["detail"] == "auth_not_configured"
    finally:
        client.app.state.settings.auth_jwt_secret = (
            "test-jwt-secret-0123456789abcdef0123456789abcdef"
        )
