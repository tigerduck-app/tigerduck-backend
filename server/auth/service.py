"""Login business logic for /v3 auth.

Raises HTTPException directly (the codebase keeps route-facing logic
pragmatic); the route layer is a thin adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

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
from server.syncjobs.provisioning import ensure_sync_jobs

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
    moodle_token: str | None,
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
    limiter.record(sid_key)
    limiter.record(ip_key)

    if not moodle_token:
        logger.info(
            "auth.login.moodle_token_missing",
            student_id=student_id,
            platform=device_info.platform,
            app_version=device_info.app_version,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="moodle_token_required",
        )
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
            moodle_token=moodle_token,
            moodle_private_token=moodle_private_token,
            now=now,
        )
        device = await _upsert_device(session, user=user, info=device_info, now=now)
        await ensure_sync_jobs(
            session, user_id=user.id, external_account_id=account.id
        )

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

    A token revoked by rotation and presented again comes from a client
    that lost the rotation reply, from a late duplicate of the request, or
    from a copy. `_handle_revoked_reuse` rotates it once more or refuses it
    when the reply may have been lost or duplicated, and treats any other
    reuse as theft: the whole successor chain plus every active session on
    the same device is revoked. A token revoked for another reason is
    refused and nothing else is revoked.
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
    if old.revoked_at is None and old.replaced_by_session_id is not None:
        await settle_contest(session, old, now)
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
    if old.first_used_at is None:
        old.first_used_at = now

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


# Revoked by being replaced with a newer token of the same lineage, so
# presenting one again is a reuse. A token revoked for any other reason is
# only dead.
_REPLACED_REASONS = frozenset(
    {SessionRevokedReason.rotated.value, SessionRevokedReason.superseded.value}
)


class _Reuse(Enum):
    ROTATE_AGAIN = "rotate_again"
    REFUSE = "refuse"
    THEFT = "theft"


async def _handle_revoked_reuse(
    session: AsyncSession,
    settings: Settings,
    auth_session: AuthSession,
    now: datetime,
) -> RefreshResult:
    if auth_session.revoked_reason not in _REPLACED_REASONS:
        # Logged out, removed with its device, expired or revoked with its
        # family earlier: nothing is left to contain, and revoking the
        # device's sessions would end the one a relogin has just made.
        logger.info(
            "auth.refresh.revoked_token",
            session_id=auth_session.id,
            reason=auth_session.revoked_reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session_revoked",
        )

    successor = None
    if auth_session.replaced_by_session_id is not None:
        # Locked, so a concurrent rotation or first use of the successor
        # lands before the checks below read it.
        successor = await session.get(
            AuthSession,
            auth_session.replaced_by_session_id,
            with_for_update=True,
            populate_existing=True,
        )

    outcome = _reuse_outcome(settings, auth_session, successor, now)
    if outcome is _Reuse.ROTATE_AGAIN:
        assert successor is not None  # narrowed by _reuse_outcome
        return await _rotate_again(session, settings, auth_session, successor, now)
    if outcome is _Reuse.REFUSE:
        logger.info(
            "auth.refresh.duplicate_refused",
            session_id=auth_session.id,
            successor_id=auth_session.replaced_by_session_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session_revoked",
        )

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


def _reuse_outcome(
    settings: Settings,
    token: AuthSession,
    successor: AuthSession | None,
    now: datetime,
) -> _Reuse:
    """What a rotated `token` presented again gets.

    It is rotated once more while nothing has used its successor: the client
    lost the reply, or this is a late duplicate of the request that rotated
    it. Inside the grace, with the successor in use, it is that late
    duplicate, or a copy, and is refused without revoking anything. Any
    other reuse is theft.
    """
    if (
        token.revoked_reason != SessionRevokedReason.rotated.value
        or token.revoked_at is None
        or token.rerotated_at is not None
        # Set when a reuse of it took the theft path, or by a grace retry
        # from before rerotated_at.
        or token.reuse_detected_at is not None
        or successor is None
        or successor.revoked_at is not None
    ):
        return _Reuse.THEFT
    since_rotation = now - token.revoked_at
    grace = timedelta(seconds=settings.auth_refresh_reuse_grace_seconds)
    window = max(
        grace, timedelta(seconds=settings.auth_refresh_recovery_window_seconds)
    )
    if successor.first_used_at is None:
        return _Reuse.ROTATE_AGAIN if since_rotation <= window else _Reuse.THEFT
    return _Reuse.REFUSE if since_rotation < grace else _Reuse.THEFT


async def _rotate_again(
    session: AsyncSession,
    settings: Settings,
    token: AuthSession,
    successor: AuthSession,
    now: datetime,
) -> RefreshResult:
    """Rotate `token` once more, for a client that may not hold `successor`.

    A late duplicate of the request that made `successor` reaches here too,
    so the client may hold it after all. `successor` and the new session
    therefore both stay live, each pointing at the other, and whichever is
    used first supersedes the other (`settle_contest`).
    """
    assert token.revoked_at is not None  # checked by _reuse_outcome
    since_rotation = now - token.revoked_at
    result = await _rotate(session, settings, token, now)
    replacement = await session.get(AuthSession, token.replaced_by_session_id)
    assert replacement is not None  # flushed by _rotate
    successor.replaced_by_session_id = replacement.id
    replacement.replaced_by_session_id = successor.id
    token.rerotated_at = now
    grace = timedelta(seconds=settings.auth_refresh_reuse_grace_seconds)
    logger.info(
        "auth.refresh.grace_retry"
        if since_rotation < grace
        else "auth.refresh.lost_rotation_recovered",
        session_id=token.id,
        successor_id=successor.id,
        replacement_id=replacement.id,
        seconds_since_rotation=int(since_rotation.total_seconds()),
    )
    return result


async def settle_contest(
    session: AsyncSession, winner: AuthSession, now: datetime
) -> None:
    """`winner`, one of the two live sessions a second rotation left, has
    been used, so the other is superseded and points at it.

    The caller holds `winner`'s row lock. A token of the superseded session
    that turns up later takes the theft path, and the chain walk goes on
    from it to `winner`.
    """
    rival = await session.get(
        AuthSession,
        winner.replaced_by_session_id,
        with_for_update=True,
        populate_existing=True,
    )
    winner.replaced_by_session_id = None
    if (
        rival is None
        or rival.revoked_at is not None
        or rival.replaced_by_session_id != winner.id
    ):
        return
    rival.revoked_at = now
    rival.revoked_reason = SessionRevokedReason.superseded.value
    logger.info(
        "auth.session.superseded", session_id=rival.id, winner_id=winner.id
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
        # The external account uniquely owns this
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
    moodle_token: str,
    moodle_private_token: str | None,
    now: datetime,
) -> None:
    # Token-only: no NTUST password is stored. The Moodle wstoken is the
    # only credential the sync worker uses. When it expires or gets
    # revoked, the sync job disables and a push notification asks the user
    # to open the app (which sends a fresh token via PATCH /auth/credentials).
    payload = {
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
    # Clean up stale device rows: if the same physical device
    # (client_device_id) was previously registered under a different user,
    # delete those rows. The CASCADE wipes tokens, sessions, and push
    # deliveries tied to that old device entry.
    old_devices = (
        await session.execute(
            select(UserDevice).where(
                UserDevice.client_device_id == info.client_device_id,
                UserDevice.user_id != user.id,
            )
        )
    ).scalars().all()
    for old in old_devices:
        logger.info(
            "auth.device.cross_user_cleanup",
            client_device_id=info.client_device_id,
            old_user_id=str(old.user_id),
            old_device_id=str(old.id),
            new_user_id=str(user.id),
        )
        await session.delete(old)
    if old_devices:
        await session.flush()

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
    if info.app_version is not None:
        device.app_version = info.app_version
    if info.os_version is not None:
        device.os_version = info.os_version
    if info.device_model is not None:
        device.device_model = info.device_model
    device.deleted_at = None
    device.last_seen_at = now
    device.last_login_at = now
    await session.flush()
    return device
