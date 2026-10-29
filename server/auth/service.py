"""Login business logic for /v3 auth.

Raises HTTPException directly (the codebase keeps route-facing logic
pragmatic); the route layer is a thin adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

import structlog
from fastapi import HTTPException, status
from sqlalchemy import delete, exists, func, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

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
from server.auth.moodle import MoodleVerifier, display_name_from
from server.auth.rate_limit import SlidingWindowLimiter
from server.auth.schemas import DeviceInfo
from server.auth.tokens import (
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
)
from server.config import Settings
from server.db import in_array, session_scope
from server.syncjobs.provisioning import ensure_sync_jobs

logger = structlog.get_logger(__name__)

# Namespace ("TDSI") of the per-device sign-in lock. Its two-int4 key form
# cannot collide with the one-bigint advisory locks used elsewhere.
_DEVICE_SIGN_IN_LOCK = 0x54445349

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
        # A 401 tells the apps the token was refused, and they may harvest a
        # fresh one with the stored password; an outage must not.
        if result.error == "unreachable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="moodle_unreachable",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="moodle_verification_failed",
        )

    now = datetime.now(UTC)
    try:
        # One sign-in per physical device at a time, so the owners read below
        # are still its owners when `_upsert_device` takes it over.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    _DEVICE_SIGN_IN_LOCK, func.hashtext(device_info.client_device_id)
                )
            )
        )
        user = await _find_or_create_user(session, student_id=student_id, now=now)
        # Also the accounts of other students on this device, whose rows
        # `_upsert_device` deletes.
        previous_owners = (
            await session.execute(
                select(UserDevice.user_id).where(
                    UserDevice.client_device_id == device_info.client_device_id,
                    UserDevice.user_id != user.id,
                )
            )
        ).scalars().all()
        await lock_accounts(session, user.id, *previous_owners)
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
        # Before the device row: a job run holds its job row while it waits
        # for the sync lock, and a sync writer holds that lock for the device.
        await ensure_sync_jobs(
            session, user_id=user.id, external_account_id=account.id
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
        user.display_name = display_name_from(result.fullname) or user.display_name
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

    A token replaced by a rotation and presented again comes from a client
    that lost the reply, from a late duplicate of the request, or from a
    copy. `_handle_revoked_reuse` rotates it again while nothing has used a
    session it was rotated into, and refuses it once one has; nothing else
    is revoked either way. A token revoked for another reason is refused.
    """
    if not settings.auth_jwt_secret or not settings.auth_refresh_hmac_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth_not_configured",
        )

    token_hash = hash_refresh_token(settings.auth_refresh_hmac_key, refresh_token)
    found_id = await session.scalar(
        select(AuthSession.id).where(AuthSession.refresh_token_hash == token_hash)
    )
    if found_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_refresh_token",
        )
    # Row locks: two concurrent refreshes with the same token serialize here;
    # the later one then sees revoked_at set and is rotated again or refused,
    # instead of forking the chain or 500ing on the unique index.
    await lock_contest(session, found_id)
    auth_session = await session.get(AuthSession, found_id, populate_existing=True)
    if auth_session is None:  # deleted with its user between the read and the lock
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_refresh_token",
        )

    now = datetime.now(UTC)

    if auth_session.revoked_at is not None:
        return await _handle_revoked_reuse(session, settings, auth_session, now)

    if auth_session.expires_at <= now:
        # A session in a ring (`_rotate_again`) stays live: revoked, it would
        # end the walk from its rivals before the walk reaches the rest.
        if auth_session.replaced_by_session_id is None:
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

# How many of the sessions one token was rotated into stay live and unused.
# The app holds one of the newest; without a limit, a copy or a broken client
# sending the token every few seconds would keep adding live sessions.
_UNUSED_SESSIONS_KEPT = 16


async def _handle_revoked_reuse(
    session: AsyncSession,
    settings: Settings,
    auth_session: AuthSession,
    now: datetime,
) -> RefreshResult:
    """A revoked token presented again.

    One a rotation replaced is rotated again while nothing has used a
    session it was rotated into: its client lost every reply, or a request
    that rotated it came late. The newest `_UNUSED_SESSIONS_KEPT` of those
    stay live. Once one of them has been used, or the token lost to a
    rival, it is refused. Nothing else is revoked either way.

    This gives up containing a copied token. A copy sent while the client
    has not used its pair gets a pair of its own; once either pair is used,
    the other is refused, and the one used keeps working until it expires
    or its device is removed. Revoking every session of the device on a
    reuse signed phones out, some for good, while no reuse looked like an
    attack (#123).
    """
    if auth_session.revoked_reason not in _REPLACED_REASONS:
        # Logged out, removed with its device, expired or revoked with its
        # family by an earlier release: revoking the device's sessions would
        # end the one a relogin has just made.
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
        # Locked by `refresh`, so a rotation or first use of the successor
        # in flight lands before the checks below read it.
        successor = await session.get(
            AuthSession,
            auth_session.replaced_by_session_id,
            populate_existing=True,
        )

    if _may_rotate_again(auth_session, successor):
        if auth_session.expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh_token_expired",
            )
        assert successor is not None  # narrowed by _may_rotate_again
        return await _rotate_again(session, settings, auth_session, successor, now)

    assert auth_session.revoked_at is not None  # revoked, as the caller checked
    since_rotation = now - auth_session.revoked_at
    if since_rotation < timedelta(seconds=settings.auth_refresh_reuse_grace_seconds):
        logger.info(
            "auth.refresh.duplicate_refused",
            session_id=auth_session.id,
            successor_id=auth_session.replaced_by_session_id,
        )
    else:
        auth_session.reuse_detected_at = now
        logger.warning(
            "auth.refresh.reuse_detected",
            session_id=auth_session.id,
            user_id=str(auth_session.user_id),
            cause=_refusal_cause(auth_session, successor),
            seconds_since_rotation=int(since_rotation.total_seconds()),
        )
        # The session dependency rolls back on exceptions, and the mark is
        # what the Token reuse panel counts.
        await session.commit()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="session_revoked",
    )


def _may_rotate_again(token: AuthSession, successor: AuthSession | None) -> bool:
    """Whether nothing has used a session the rotated `token` was rotated
    into.

    `successor` is the newest of them. A use of any of them supersedes the
    others, and a rotation of `successor` revokes it, so it is live and
    unused only while none has been used.
    """
    return (
        token.revoked_reason == SessionRevokedReason.rotated.value
        and successor is not None
        and successor.revoked_at is None
        and successor.first_used_at is None
    )


def _refusal_cause(token: AuthSession, successor: AuthSession | None) -> str:
    if token.revoked_reason == SessionRevokedReason.superseded.value:
        return "superseded"
    if successor is not None and (
        successor.first_used_at is not None
        or successor.revoked_reason in _REPLACED_REASONS
    ):
        return "successor_used"
    return "successor_revoked"


async def _rotate_again(
    session: AsyncSession,
    settings: Settings,
    token: AuthSession,
    successor: AuthSession,
    now: datetime,
) -> RefreshResult:
    """Rotate `token` again, for a client that may not hold `successor`.

    A late duplicate of a request that rotated it reaches here too, so the
    client may hold `successor`, or another live session `token` was
    rotated into, after all. They stay live, linked in a ring, and the
    first of them used supersedes the rest (`settle_contest`). The new
    session joins the ring after `successor`, and the oldest beyond
    `_UNUSED_SESSIONS_KEPT` are superseded.
    """
    assert token.revoked_at is not None  # checked by _may_rotate_again
    since_rotation = now - token.revoked_at
    result = await _rotate(session, settings, token, now)
    replacement = await session.get(AuthSession, token.replaced_by_session_id)
    assert replacement is not None  # flushed by _rotate
    replacement.replaced_by_session_id = (
        successor.id
        if successor.replaced_by_session_id is None
        else successor.replaced_by_session_id
    )
    successor.replaced_by_session_id = replacement.id
    # Each session joins after the newest, so the ring runs in id order from
    # the oldest, and cutting the oldest leaves it closed at the newest.
    ring = sorted(await _linked_ids(session, replacement.id))
    dropped: list[int] = []
    if len(ring) > _UNUSED_SESSIONS_KEPT:
        replacement.replaced_by_session_id = ring[-_UNUSED_SESSIONS_KEPT]
        dropped = await _supersede(
            session, ring[:-_UNUSED_SESSIONS_KEPT], replacement.id, now
        )
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
        superseded_ids=sorted(dropped),
    )
    return result


async def _linked_ids(session: AsyncSession, session_id: int) -> set[int]:
    """`session_id`, the session it points at and, on from there, each live
    session's rival: what a refresh or a first use of it reads and settles."""
    linked = (
        select(
            AuthSession.id,
            AuthSession.replaced_by_session_id.label("next_id"),
            true().label("live"),
        )
        .where(AuthSession.id == session_id)
        .cte("linked", recursive=True)
    )
    previous = linked.alias()
    rival = aliased(AuthSession)
    linked = linked.union(
        select(
            rival.id, rival.replaced_by_session_id, rival.revoked_at.is_(None)
        ).where(rival.id == previous.c.next_id, previous.c.live)
    )
    return set((await session.execute(select(linked.c.id))).scalars())


async def lock_contest(session: AsyncSession, session_id: int) -> None:
    """Lock `session_id` and the sessions linked to it (`_linked_ids`), in
    id order.

    A refresh and a first use both lock this way, so they cannot deadlock
    on the sessions one token was rotated into. A session that joins while
    this waits is newer than every one locked, so it is locked after them.
    """
    locked: set[int] = set()
    while missing := await _linked_ids(session, session_id) - locked:
        await session.execute(
            select(AuthSession.id)
            .where(in_array(AuthSession.id, missing))
            .order_by(AuthSession.id)
            .with_for_update()
        )
        locked |= missing


async def settle_contest(
    session: AsyncSession, winner: AuthSession, now: datetime
) -> None:
    """`winner`, one of the live sessions that rotations of one token left,
    has been used, so the others are superseded and point at it.

    The caller holds their locks (`lock_contest`). A token of a superseded
    session that turns up later is refused.
    """
    rivals = await _linked_ids(session, winner.id) - {winner.id}
    winner.replaced_by_session_id = None
    if not rivals:
        return
    superseded = await _supersede(session, rivals, winner.id, now)
    if superseded:
        logger.info(
            "auth.session.superseded",
            session_ids=sorted(superseded),
            winner_id=winner.id,
        )


async def _supersede(
    session: AsyncSession, ids: Iterable[int], by: int, now: datetime
) -> list[int]:
    """Revoke the live sessions among `ids` as superseded by `by`, and
    return their ids. A used one is skipped: it is in no ring."""
    rows = await session.execute(
        update(AuthSession)
        .where(
            in_array(AuthSession.id, ids),
            AuthSession.revoked_at.is_(None),
            AuthSession.first_used_at.is_(None),
        )
        .values(
            revoked_at=now,
            revoked_reason=SessionRevokedReason.superseded.value,
            replaced_by_session_id=by,
        )
        .returning(AuthSession.id)
    )
    return list(rows.scalars())


async def logout(session: AsyncSession, *, session_id: int) -> None:
    auth_session = await session.get(AuthSession, session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        return  # already gone — logout is idempotent
    now = datetime.now(UTC)
    auth_session.revoked_at = now
    auth_session.revoked_reason = SessionRevokedReason.logout.value
    auth_session.last_used_at = now
    logger.info("auth.logout", session_id=session_id)


async def store_display_name(
    session: AsyncSession, user_id: uuid.UUID, fullname: str | None
) -> None:
    """Store Moodle's full name for the user. A blank one keeps the stored
    name, and an unchanged one leaves the row unwritten."""
    name = display_name_from(fullname)
    if name is not None:
        await session.execute(
            update(User)
            .where(User.id == user_id, User.display_name.is_distinct_from(name))
            .values(display_name=name)
        )


async def try_store_display_name(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    fullname: str | None,
) -> None:
    """`store_display_name` in a transaction of its own, after the caller's
    work committed, so a failure costs only the name."""
    try:
        async with session_scope(factory) as session:
            await store_display_name(session, user_id, fullname)
    except Exception as exc:
        # Only the type: the error renders the statement's parameters, the name too.
        logger.warning(
            "auth.display_name.not_stored",
            user_id=str(user_id),
            error=type(exc).__name__,
        )


async def lock_accounts(session: AsyncSession, *user_ids: uuid.UUID) -> None:
    """Lock the students' accounts, lowest id first, until the transaction
    ends. Every path that signs a device in or out takes them before it
    touches a device or its sessions, so no two of them can deadlock."""
    await session.execute(
        select(ExternalAccount.id)
        .where(in_array(ExternalAccount.user_id, user_ids))
        .order_by(ExternalAccount.id)
        .with_for_update()
    )


async def forget_credentials_once_signed_out(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Drop the stored Moodle token once the user has no device left: the
    apps delete the device on sign-out, and the next sign-in stores a fresh
    token (tigerduck-backend#85).

    Decides under the student's account lock (`lock_accounts`, which the
    callers already hold), so a sign-out racing another sign-out or a
    sign-in decides by the devices they committed."""
    await session.flush()
    await lock_accounts(session, user_id)
    await session.execute(
        delete(ExternalAccountCredential).where(
            ExternalAccountCredential.external_account_id.in_(
                select(ExternalAccount.id).where(ExternalAccount.user_id == user_id)
            ),
            ~exists().where(
                UserDevice.user_id == user_id, UserDevice.deleted_at.is_(None)
            ),
        )
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
    for old_user_id in {old.user_id for old in old_devices}:
        await forget_credentials_once_signed_out(session, old_user_id)

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
