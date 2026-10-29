"""End-to-end tests for POST /v3/auth/login."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import structlog
from sqlalchemy import select
from structlog.testing import capture_logs

from server.auth import service
from server.auth.crypto import CredentialCipher, EncryptedBlob
from server.auth.models import (
    AuthSession,
    ExternalAccount,
    ExternalAccountCredential,
    User,
    UserDevice,
)
from server.auth.moodle import (
    HttpMoodleVerifier,
    MoodleVerifyResult,
    StaticMoodleVerifier,
)
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


def allow_moodle(
    client, username: str = "b11015000", fullname: str | None = None
) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username=username, fullname=fullname)
    )


def deny_moodle(
    client, error: str = "invalid_token", fullname: str | None = None
) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=False, error=error, fullname=fullname)
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
        # Token-only: no password stored.
        assert "ntust_password" not in decrypted
        assert "password_verified" not in decrypted
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


async def test_android_login_stores_token_only_credentials(client) -> None:
    """All platforms store credentials (token-only, no password) and
    get sync jobs for cross-device sync."""
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
        assert "ntust_password" not in decrypted
        assert decrypted["token_cache"]["moodle_token"] == "moodle-tok-1"
        # Sync jobs provisioned for Android too.
        jobs = (
            await session.execute(
                select(SyncJob).where(SyncJob.user_id == user.id)
            )
        ).scalars().all()
        assert len(jobs) >= 1


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


@pytest.mark.parametrize("error", ["invalid_token", "username_mismatch"])
async def test_moodle_failure_returns_401_and_creates_nothing(client, error) -> None:
    deny_moodle(client, error=error)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 401
    assert response.json()["detail"] == "moodle_verification_failed"

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []


async def test_unreachable_moodle_returns_503_and_creates_nothing(client) -> None:
    """An outage says nothing about the token, so the apps try again later
    instead of taking it for refused."""
    deny_moodle(client, error="unreachable")
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 503
    assert response.json()["detail"] == "moodle_unreachable"

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []


async def test_moodle_in_maintenance_returns_503(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "exception": "moodle_exception",
                "errorcode": "sitemaintenance",
                "message": "Site is under maintenance",
            },
        )

    client.app.state.moodle_verifier = HttpMoodleVerifier(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 503
    assert response.json()["detail"] == "moodle_unreachable"


@pytest.mark.parametrize(
    ("error", "status"), [("invalid_token", 401), ("unreachable", 503)]
)
async def test_login_rate_limited_after_max_attempts(client, error, status) -> None:
    deny_moodle(client, error=error)
    for _ in range(5):
        response = await client.post("/v3/auth/login", json=LOGIN_BODY)
        assert response.status_code == status
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 429
    assert response.json()["detail"] == "too_many_login_attempts"


def tokenless_body(student_id: str = "B11015000", **token: object) -> dict:
    """LOGIN_BODY, password included, with no Moodle token unless `token`
    sets one."""
    body = {
        key: value
        for key, value in LOGIN_BODY.items()
        if key not in ("moodle_token", "moodle_private_token")
    }
    return {**body, **token, "student_id": student_id}


@pytest.mark.parametrize(
    "token",
    [{}, {"moodle_token": None}, {"moodle_token": ""}],
    ids=["missing", "null", "empty"],
)
async def test_password_without_token_is_refused(client, token) -> None:
    allow_moodle(client)
    response = await client.post("/v3/auth/login", json=tokenless_body(**token))
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "moodle_token_required"

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []


@pytest.mark.parametrize("shared", ["student_id", "ip"])
async def test_tokenless_attempts_count_against_the_limit(
    client, monkeypatch, shared
) -> None:
    # Only the key under test repeats, so the 429 can come from it alone.
    monkeypatch.setattr(client.app.state.settings, "auth_trust_forwarded_for", True)
    allow_moodle(client)
    for n in range(client.app.state.settings.auth_login_max_attempts):
        student_id = "B11015000" if shared == "student_id" else f"B1101510{n}"
        ip = "10.0.0.1" if shared == "ip" else f"10.0.1.{n}"
        response = await client.post(
            "/v3/auth/login",
            json=tokenless_body(student_id),
            headers={"X-Forwarded-For": ip},
        )
        assert response.status_code == 400, response.text

    ip = "10.0.0.1" if shared == "ip" else "10.0.2.1"
    response = await client.post(
        "/v3/auth/login", json=LOGIN_BODY, headers={"X-Forwarded-For": ip}
    )
    assert response.status_code == 429
    assert response.json()["detail"] == "too_many_login_attempts"


async def test_tokenless_login_is_logged(client, monkeypatch) -> None:
    allow_moodle(client)
    # The module's logger may be cached with the processors of an earlier
    # app, which capture_logs does not reach; a new one binds inside it.
    monkeypatch.setattr(service, "logger", structlog.get_logger(service.__name__))
    with capture_logs() as logs:
        response = await client.post("/v3/auth/login", json=tokenless_body())
    assert response.status_code == 400, response.text
    assert {
        "event": "auth.login.moodle_token_missing",
        "student_id": "B11015000",
        "platform": "ios",
        "app_version": "3.0.0",
        "log_level": "info",
    } in logs


@pytest.mark.parametrize("password", [None, "x" * 300], ids=["null", "long"])
async def test_token_login_ignores_the_password(client, password) -> None:
    allow_moodle(client)
    response = await client.post(
        "/v3/auth/login", json={**LOGIN_BODY, "password": password}
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["student_id"] == "B11015000"


async def test_login_reactivates_soft_deleted_user(client) -> None:
    # Pre-seed a soft-deleted user owning the external account.
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


async def _stored_display_name(client) -> str | None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return await session.scalar(
            select(User.display_name).where(User.student_id == "B11015000")
        )


async def test_sign_in_stores_the_moodle_full_name(client) -> None:
    allow_moodle(client, fullname="  Test Student  ")
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["display_name"] == "Test Student"
    assert await _stored_display_name(client) == "Test Student"

    allow_moodle(client, fullname="Renamed Student")
    await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert await _stored_display_name(client) == "Renamed Student"


@pytest.mark.parametrize(
    ("fullname", "stored"),
    [
        ("B11015000 @ 王小明", "王小明"),
        ("B11015000@Test Student", "Test Student"),
        ("B11015000 @ ", "B11015000 @"),
    ],
    ids=["spaced", "unspaced", "nothing-after"],
)
async def test_sign_in_stores_the_name_after_the_student_id(
    client, fullname, stored
) -> None:
    allow_moodle(client, fullname=fullname)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["display_name"] == stored
    assert await _stored_display_name(client) == stored


@pytest.mark.parametrize("fullname", [None, "", "   "])
async def test_a_blank_or_missing_full_name_keeps_the_stored_one(
    client, fullname
) -> None:
    allow_moodle(client, fullname="Test Student")
    await client.post("/v3/auth/login", json=LOGIN_BODY)

    allow_moodle(client, fullname=fullname)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["display_name"] == "Test Student"
    assert await _stored_display_name(client) == "Test Student"


@pytest.mark.parametrize(
    ("fullname", "stored"),
    [("名" * 200, "名" * 128), ("a" * 127 + " b", "a" * 127)],
    ids=["cut", "no-trailing-space-after-cut"],
)
async def test_a_long_full_name_is_cut_to_the_column(
    client, fullname, stored
) -> None:
    allow_moodle(client, fullname=fullname)
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    assert await _stored_display_name(client) == stored


# Postgres refuses a NUL in text, so the name write fails for real.
_REFUSED_NAME = "Test\x00Student"


async def test_a_name_the_database_refuses_still_signs_in(
    client, monkeypatch
) -> None:
    allow_moodle(client, fullname=_REFUSED_NAME)
    monkeypatch.setattr(service, "logger", structlog.get_logger(service.__name__))
    with capture_logs() as logs:
        response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["display_name"] is None

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        user = (await session.execute(select(User))).scalar_one()
        assert user.display_name is None
        assert user.last_login_at is not None
        assert (await session.execute(select(AuthSession))).scalars().all()
    assert any(e["event"] == "auth.display_name.not_stored" for e in logs)
    assert "Student" not in repr(logs)


@pytest.mark.parametrize("error", ["invalid_token", "username_mismatch"])
async def test_a_refused_sign_in_stores_no_name(client, error) -> None:
    allow_moodle(client, fullname="Test Student")
    await client.post("/v3/auth/login", json=LOGIN_BODY)

    deny_moodle(client, error=error, fullname="Other Student")
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 401
    assert await _stored_display_name(client) == "Test Student"
