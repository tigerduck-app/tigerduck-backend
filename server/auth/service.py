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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    AuthSession,
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    SessionRevokedReason,
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


@dataclass(frozen=True)
class RefreshResult:
    access_token: str
    refresh_token: str
    expires_in: int


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
    try:
        user = await _find_or_create_user(session, student_id=student_id, now=now)
        account = await _upsert_external_account(
            session, user=user, student_id=student_id
        )
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
    except IntegrityError as exc:
        # Two first-logins for the same student racing on the partial
        # unique indexes: the loser gets a clean 409 and the client simply
        # retries (the winner's rows are then found by the lookup paths).
        logger.info("auth.login.conflict_retry", student_id=student_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="login_conflict_retry",
        ) from exc

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


async def refresh(
    session: AsyncSession, settings: Settings, *, refresh_token: str
) -> RefreshResult:
    """Rotate a refresh token.

    Reuse semantics (security review 1.5): a token revoked by rotation can
    be redeemed again within `auth_refresh_reuse_grace_seconds` IF its
    successor was never used — that's a client retrying after losing the
    rotation response. The lost successor is revoked and a fresh session is
    chained in its place. Any other reuse is treated as theft: the whole
    successor chain plus every active session on the same device is revoked.
    """
    if not settings.auth_jwt_secret or not settings.auth_refresh_hmac_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth_not_configured",
        )

    token_hash = hash_refresh_token(settings.auth_refresh_hmac_key, refresh_token)
    # Row lock: two concurrent refreshes with the same token serialize here;
    # the loser then sees revoked_at set and flows through the grace path
    # instead of forking the rotation chain or 500ing on the unique index.
    auth_session = (
        await session.execute(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == token_hash)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if auth_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_refresh_token",
        )

    now = datetime.now(UTC)

    if auth_session.revoked_at is not None:
        return await _handle_revoked_reuse(session, settings, auth_session, now)

    if auth_session.expires_at <= now:
        auth_session.revoked_at = now
        auth_session.revoked_reason = SessionRevokedReason.expired.value
        # The route's session dependency rolls back on exceptions — commit
        # explicitly so the revocation outlives the 401 we're about to raise.
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh_token_expired",
        )

    return await _rotate(session, settings, auth_session, now)


async def _rotate(
    session: AsyncSession,
    settings: Settings,
    old: AuthSession,
    now: datetime,
) -> RefreshResult:
    new_refresh_token = generate_refresh_token()
    new_session = AuthSession(
        user_id=old.user_id,
        device_id=old.device_id,
        refresh_token_hash=hash_refresh_token(
            settings.auth_refresh_hmac_key, new_refresh_token
        ),
        expires_at=now + timedelta(days=settings.auth_refresh_token_ttl_days),
        last_used_at=now,
    )
    session.add(new_session)
    await session.flush()

    old.revoked_at = now
    old.revoked_reason = SessionRevokedReason.rotated.value
    old.replaced_by_session_id = new_session.id
    old.last_used_at = now

    access_token = issue_access_token(
        settings.auth_jwt_secret,
        user_id=str(old.user_id),
        session_id=new_session.id,
        device_id=str(old.device_id) if old.device_id else None,
        ttl_seconds=settings.auth_access_token_ttl_seconds,
        now=now,
    )
    return RefreshResult(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.auth_access_token_ttl_seconds,
    )


async def _handle_revoked_reuse(
    session: AsyncSession,
    settings: Settings,
    auth_session: AuthSession,
    now: datetime,
) -> RefreshResult:
    grace = timedelta(seconds=settings.auth_refresh_reuse_grace_seconds)
    successor = None
    if auth_session.replaced_by_session_id is not None:
        successor = await session.get(
            AuthSession, auth_session.replaced_by_session_id
        )

    is_benign_retry = (
        auth_session.revoked_reason == SessionRevokedReason.rotated.value
        and auth_session.revoked_at is not None
        and auth_session.revoked_at > now - grace
        and successor is not None
        and successor.revoked_at is None
    )
    if is_benign_retry:
        assert successor is not None
        # The successor token was lost in transit — kill it and chain a
        # fresh session in its place.
        successor.revoked_at = now
        successor.revoked_reason = SessionRevokedReason.rotated.value
        logger.info(
            "auth.refresh.grace_retry",
            session_id=auth_session.id,
            lost_successor_id=successor.id,
        )
        return await _rotate(session, settings, auth_session, now)

    auth_session.reuse_detected_at = now
    await _revoke_family(session, auth_session, now)
    logger.warning(
        "auth.refresh.reuse_detected",
        session_id=auth_session.id,
        user_id=str(auth_session.user_id),
    )
    # Commit before raising: the session dependency rolls back on exceptions,
    # and losing the family revocation would let the stolen chain live on.
    await session.commit()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="refresh_reuse_detected",
    )


async def _revoke_family(
    session: AsyncSession, start: AuthSession, now: datetime
) -> None:
    """Revoke the forward successor chain of `start`, plus every active
    session on the same device. The chain walk does not depend on the
    nullable device_id, so theft is contained even for device-less rows."""
    seen = {start.id}
    current = start
    while current.replaced_by_session_id is not None:
        nxt = await session.get(AuthSession, current.replaced_by_session_id)
        if nxt is None or nxt.id in seen:
            break
        if nxt.revoked_at is None:
            nxt.revoked_at = now
            nxt.revoked_reason = SessionRevokedReason.reuse_detected.value
        seen.add(nxt.id)
        current = nxt

    if start.device_id is not None:
        device_sessions = (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.device_id == start.device_id,
                    AuthSession.revoked_at.is_(None),
                )
            )
        ).scalars()
        for row in device_sessions:
            row.revoked_at = now
            row.revoked_reason = SessionRevokedReason.reuse_detected.value


async def logout(session: AsyncSession, *, session_id: int) -> None:
    auth_session = await session.get(AuthSession, session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        return  # already gone — logout is idempotent
    now = datetime.now(UTC)
    auth_session.revoked_at = now
    auth_session.revoked_reason = SessionRevokedReason.logout.value
    auth_session.last_used_at = now
    logger.info("auth.logout", session_id=session_id)


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
