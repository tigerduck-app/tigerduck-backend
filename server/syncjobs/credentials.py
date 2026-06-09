"""Credential blob access for the sync worker + the password iron rule.

Security review 1.4 — THE iron rule: the stored NTUST password may be
presented to SSO at most once per run, and an auth-class rejection
(`SsoAuthFailed`) immediately invalidates the credential and disables all
of the user's sync jobs. It is NEVER retried — repeated wrong-password
attempts could lock the student's school account. Network-class failures
(`SsoUnavailable`) propagate to the executor's normal backoff path; SSO
never evaluated the password, so no attempt was consumed.

Durability: the rule must hold even if the process dies (or the DB
errors) between the SSO call and the disable bookkeeping. So a token
refresh is a three-transaction sequence (`refresh_moodle_token_durably`):

1. commit an `sso_attempt_pending` marker into the blob,
2. make the single SSO attempt,
3. commit the outcome (fresh token, or marker-clear on network error).

If step 3 is ever lost, the committed marker makes the next
`load_credential_blob` raise `CredentialInvalid` instead of allowing a
second password attempt — safety over availability.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.crypto import (
    CredentialCipher,
    CredentialCipherError,
    EncryptedBlob,
    build_credential_aad,
)
from server.auth.models import (
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
)
from server.db import session_scope
from server.syncjobs.models import SyncJob, SyncJobStatus
from server.syncjobs.moodle_client import (
    SsoAuthFailed,
    SsoUnavailable,
    TokenObtainer,
)

logger = structlog.get_logger(__name__)

REAUTH_SCENARIO = "reauth_required"
SSO_ATTEMPT_MARKER = "sso_attempt_pending"


class CredentialInvalid(Exception):
    """Credentials unusable — sync for this account must be disabled."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def load_credential_blob(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    external_account_id: int,
) -> tuple[ExternalAccount, dict]:
    account = await session.get(ExternalAccount, external_account_id)
    if account is None:
        raise CredentialInvalid("account_missing")
    if account.credential_status != CredentialStatus.active.value:
        raise CredentialInvalid(f"credential_status_{account.credential_status}")
    credential = await session.get(ExternalAccountCredential, external_account_id)
    if credential is None:
        raise CredentialInvalid("credential_missing")
    try:
        blob = cipher.decrypt(
            EncryptedBlob(
                key_id=credential.encryption_key_id,
                nonce=credential.nonce,
                ciphertext=credential.ciphertext,
                aad=credential.aad,
            )
        )
    except CredentialCipherError as exc:
        logger.error(
            "syncjobs.credentials.decrypt_failed",
            external_account_id=external_account_id,
        )
        raise CredentialInvalid("decrypt_failed") from exc
    if blob.get(SSO_ATTEMPT_MARKER):
        # A previous SSO attempt's outcome was never durably recorded.
        # The iron rule forbids guessing — refuse without another attempt;
        # a re-login rewrites the blob and clears the marker.
        raise CredentialInvalid("sso_attempt_unresolved")
    credential.last_used_at = datetime.now(UTC)
    return account, blob


async def _store_blob(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    account: ExternalAccount,
    blob: dict,
    rotated_at: datetime | None = None,
) -> None:
    encrypted = cipher.encrypt(
        blob, build_credential_aad(account.id, account.provider)
    )
    credential = await session.get(ExternalAccountCredential, account.id)
    if credential is None:
        raise CredentialInvalid("credential_missing")
    credential.encryption_key_id = encrypted.key_id
    credential.ciphertext = encrypted.ciphertext
    credential.nonce = encrypted.nonce
    credential.aad = encrypted.aad
    if rotated_at is not None:
        credential.rotated_at = rotated_at


async def refresh_moodle_token_durably(
    session_factory: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    obtainer: TokenObtainer,
    *,
    external_account_id: int,
) -> str:
    """One SSO attempt to mint a fresh Moodle token, with the iron rule
    enforced durably (see module docstring): the attempt marker commits
    BEFORE the SSO call, and the outcome commits in its own transaction
    so a later fetch failure cannot roll the fresh token back."""
    async with session_scope(session_factory) as session:
        account, blob = await load_credential_blob(
            session, cipher, external_account_id=external_account_id
        )
        password = blob.get("ntust_password")
        if not isinstance(password, str) or not password:
            raise CredentialInvalid("password_missing")
        username = account.external_user_id
        await _store_blob(
            session,
            cipher,
            account=account,
            blob={**blob, SSO_ATTEMPT_MARKER: True},
        )

    try:
        obtained = await obtainer.obtain_token(
            username=username, password=password
        )
    except SsoAuthFailed as exc:
        # Marker stays committed: even if the caller's disable bookkeeping
        # is lost, no future run can re-attempt this password.
        raise CredentialInvalid("sso_rejected") from exc
    except SsoUnavailable:
        # Network-class: the password was never evaluated, so re-open the
        # retry path by clearing the marker. If THIS write fails the marker
        # survives and the next run conservatively disables instead of
        # re-attempting — safety over availability.
        async with session_scope(session_factory) as session:
            account = await session.get(ExternalAccount, external_account_id)
            if account is not None:
                await _store_blob(session, cipher, account=account, blob=blob)
        raise

    now = datetime.now(UTC)
    new_blob = {k: v for k, v in blob.items() if k != SSO_ATTEMPT_MARKER}
    new_blob["password_verified"] = True
    new_blob["token_cache"] = {
        "moodle_token": obtained.token,
        "moodle_private_token": obtained.private_token,
        "obtained_at": now.isoformat(),
    }
    async with session_scope(session_factory) as session:
        account = await session.get(ExternalAccount, external_account_id)
        if account is None:
            raise CredentialInvalid("account_missing")
        await _store_blob(
            session, cipher, account=account, blob=new_blob, rotated_at=now
        )
        account.last_auth_success_at = now
        account.last_auth_error = None
    logger.info(
        "syncjobs.credentials.token_refreshed", account_id=external_account_id
    )
    return obtained.token


async def mark_credentials_invalid(
    session: AsyncSession,
    *,
    account: ExternalAccount,
    user_id: uuid.UUID,
    error: str,
) -> None:
    """Credential failure handling (sync-and-push spec §4): flag the
    account, disable every sync job for the user, and queue ONE system
    push (delivery pipeline activates in Phase 4; the dedupe index makes
    re-invalidation idempotent)."""
    now = datetime.now(UTC)
    account.credential_status = CredentialStatus.invalid.value
    account.last_auth_failure_at = now
    account.last_auth_error = error

    await session.execute(
        update(SyncJob)
        .where(SyncJob.user_id == user_id)
        .values(
            status=SyncJobStatus.disabled.value,
            locked_by=None,
            locked_at=None,
            last_failure_at=now,
            last_error=error,
        )
    )

    await session.execute(
        pg_insert(PushJob)
        .values(
            user_id=user_id,
            dedupe_key=f"system:account:{account.id}:{REAUTH_SCENARIO}",
            channel="system",
            scenario=REAUTH_SCENARIO,
            fire_at=now,
            payload={"reason": "credential_invalid", "provider": account.provider},
        )
        .on_conflict_do_nothing()
    )
    logger.warning(
        "syncjobs.credentials.invalidated",
        account_id=account.id,
        user_id=str(user_id),
        error=error,
    )
