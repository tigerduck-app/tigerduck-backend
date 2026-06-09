"""Login business logic for /v3 auth.

Raises HTTPException directly (the codebase keeps route-facing logic
pragmatic); the route layer is a thin adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    AuthSession,
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    User,
    UserDevice,
    UserStatus,
)
from server.auth.moodle import MoodleVerifier
from server.auth.rate_limit import SlidingWindowLimiter
from server.auth.schemas import DeviceInfo
from server.auth.tokens import (
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
)
from server.config import Settings

logger = structlog.get_logger(__name__)

PROVIDER_NTUST_SSO = "ntust_sso"


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    refresh_token: str
    expires_in: int
    user: User
    device: UserDevice


def ensure_auth_configured(
    settings: Settings, cipher: CredentialCipher | None
) -> None:
    if (
        not settings.auth_jwt_secret
        or not settings.auth_refresh_hmac_key
        or cipher is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth_not_configured",
        )


async def login(
    session: AsyncSession,
    settings: Settings,
    verifier: MoodleVerifier,
    limiter: SlidingWindowLimiter,
    cipher: CredentialCipher | None,
    *,
    student_id: str,
    password: str,
    moodle_token: str,
    moodle_private_token: str | None,
    device_info: DeviceInfo,
    client_ip: str,
) -> LoginResult:
    ensure_auth_configured(settings, cipher)
    assert cipher is not None  # narrowed by ensure_auth_configured

    sid_key = f"login:sid:{student_id.lower()}"
    ip_key = f"login:ip:{client_ip}"
    if not limiter.allow(sid_key) or not limiter.allow(ip_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too_many_login_attempts",
        )
    # Record BEFORE the Moodle call: every verification attempt counts, so
    # the endpoint can't be hammered as a token-validation oracle.
    limiter.record(sid_key)
    limiter.record(ip_key)

    result = await verifier.verify(token=moodle_token, student_id=student_id)
    if not result.ok:
        logger.info(
            "auth.login.moodle_rejected", student_id=student_id, error=result.error
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="moodle_verification_failed",
        )

    now = datetime.now(UTC)
    user = await _find_or_create_user(session, student_id=student_id, now=now)
    account = await _upsert_external_account(session, user=user, student_id=student_id)
    await _store_credentials(
        session,
        cipher=cipher,
        account=account,
        password=password,
        moodle_token=moodle_token,
        moodle_private_token=moodle_private_token,
        now=now,
    )
    device = await _upsert_device(session, user=user, info=device_info, now=now)

    refresh_token = generate_refresh_token()
    auth_session = AuthSession(
        user_id=user.id,
        device_id=device.id,
        refresh_token_hash=hash_refresh_token(
            settings.auth_refresh_hmac_key, refresh_token
        ),
        expires_at=now + timedelta(days=settings.auth_refresh_token_ttl_days),
    )
    session.add(auth_session)
    user.last_login_at = now
    await session.flush()

    access_token = issue_access_token(
        settings.auth_jwt_secret,
        user_id=str(user.id),
        session_id=auth_session.id,
        device_id=str(device.id),
        ttl_seconds=settings.auth_access_token_ttl_seconds,
        now=now,
    )

    limiter.reset(sid_key)
    limiter.reset(ip_key)
    logger.info(
        "auth.login.success",
        user_id=str(user.id),
        device_id=str(device.id),
        platform=device.platform,
    )
    return LoginResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.auth_access_token_ttl_seconds,
        user=user,
        device=device,
    )


async def _find_or_create_user(
    session: AsyncSession, *, student_id: str, now: datetime
) -> User:
    account = (
        await session.execute(
            select(ExternalAccount).where(
                ExternalAccount.provider == PROVIDER_NTUST_SSO,
                ExternalAccount.external_user_id == student_id,
            )
        )
    ).scalar_one_or_none()

    if account is None:
        user = User(student_id=student_id)
        session.add(user)
        await session.flush()
        return user

    user = (
        await session.execute(select(User).where(User.id == account.user_id))
    ).scalar_one()
    if user.status == UserStatus.suspended.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account_suspended"
        )
    if user.deleted_at is not None:
        # Security review 1.7: the external account uniquely owns this
        # student id, so a re-login after account deletion reactivates the
        # soft-deleted user (data comes back) instead of failing on the
        # (provider, external_user_id) unique constraint.
        logger.info("auth.login.reactivated_user", user_id=str(user.id))
        user.deleted_at = None
        user.status = UserStatus.active.value
    if user.student_id != student_id:
        user.student_id = student_id
    return user


async def _upsert_external_account(
    session: AsyncSession, *, user: User, student_id: str
) -> ExternalAccount:
    account = (
        await session.execute(
            select(ExternalAccount).where(
                ExternalAccount.user_id == user.id,
                ExternalAccount.provider == PROVIDER_NTUST_SSO,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        account = ExternalAccount(
            user_id=user.id,
            provider=PROVIDER_NTUST_SSO,
            external_user_id=student_id,
        )
        session.add(account)
        await session.flush()
    return account


async def _store_credentials(
    session: AsyncSession,
    *,
    cipher: CredentialCipher,
    account: ExternalAccount,
    password: str,
    moodle_token: str,
    moodle_private_token: str | None,
    now: datetime,
) -> None:
    # Security review 1.4: the backend verified the Moodle token, NOT the
    # password. Store it unverified; the Phase-3 sync worker may use it at
    # most once and must disable sync on the first SSO failure instead of
    # retrying (repeated wrong-password logins could lock the student's
    # school account).
    payload = {
        "ntust_password": password,
        "password_verified": False,
        "token_cache": {
            "moodle_token": moodle_token,
            "moodle_private_token": moodle_private_token,
            "obtained_at": now.isoformat(),
        },
    }
    blob = cipher.encrypt(
        payload, build_credential_aad(account.id, PROVIDER_NTUST_SSO)
    )

    credential = await session.get(ExternalAccountCredential, account.id)
    if credential is None:
        credential = ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id=blob.key_id,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            aad=blob.aad,
        )
        session.add(credential)
    else:
        credential.encryption_key_id = blob.key_id
        credential.ciphertext = blob.ciphertext
        credential.nonce = blob.nonce
        credential.aad = blob.aad
        credential.rotated_at = now

    account.credential_status = CredentialStatus.active.value
    account.last_auth_success_at = now
    account.last_auth_error = None


async def _upsert_device(
    session: AsyncSession, *, user: User, info: DeviceInfo, now: datetime
) -> UserDevice:
    device = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.user_id == user.id,
                UserDevice.client_device_id == info.client_device_id,
            )
        )
    ).scalar_one_or_none()
    if device is None:
        device = UserDevice(
            user_id=user.id,
            client_device_id=info.client_device_id,
            platform=info.platform,
        )
        session.add(device)
    device.platform = info.platform
    device.device_name = info.device_name
    device.app_version = info.app_version
    device.os_version = info.os_version
    device.deleted_at = None
    device.last_seen_at = now
    device.last_login_at = now
    await session.flush()
    return device
