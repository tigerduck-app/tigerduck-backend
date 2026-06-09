"""Credential blob access for the sync worker + the password iron rule.

Security review 1.4 — THE iron rule: the stored NTUST password may be
presented to SSO at most once per run, and an auth-class rejection
(`SsoAuthFailed`) immediately invalidates the credential and disables all
of the user's sync jobs. It is NEVER retried — repeated wrong-password
attempts could lock the student's school account. Network-class failures
(`SsoUnavailable`) propagate to the executor's normal backoff path; SSO
never evaluated the password, so no attempt was consumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
from server.syncjobs.models import SyncJob, SyncJobStatus
from server.syncjobs.moodle_client import SsoAuthFailed, TokenObtainer

logger = structlog.get_logger(__name__)

REAUTH_SCENARIO = "reauth_required"


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
    credential.last_used_at = datetime.now(UTC)
    return account, blob


async def refresh_moodle_token(
    session: AsyncSession,
    cipher: CredentialCipher,
    obtainer: TokenObtainer,
    *,
    account: ExternalAccount,
    blob: dict,
) -> str:
    """One SSO attempt to mint a fresh Moodle token. See module docstring
    for the retry semantics. On success the blob is re-encrypted with
    `password_verified=true` and the new token cache."""
    password = blob.get("ntust_password")
    if not isinstance(password, str) or not password:
        raise CredentialInvalid("password_missing")

    try:
        obtained = await obtainer.obtain_token(
            username=account.external_user_id, password=password
        )
    except SsoAuthFailed as exc:
        raise CredentialInvalid("sso_rejected") from exc
    # SsoUnavailable intentionally propagates — network-class, retriable.

    now = datetime.now(UTC)
    new_blob = {
        **blob,
        "password_verified": True,
        "token_cache": {
            "moodle_token": obtained.token,
            "moodle_private_token": obtained.private_token,
            "obtained_at": now.isoformat(),
        },
    }
    encrypted = cipher.encrypt(
        new_blob, build_credential_aad(account.id, account.provider)
    )
    credential = await session.get(ExternalAccountCredential, account.id)
    if credential is None:  # credential row vanished mid-run
        raise CredentialInvalid("credential_missing")
    credential.encryption_key_id = encrypted.key_id
    credential.ciphertext = encrypted.ciphertext
    credential.nonce = encrypted.nonce
    credential.aad = encrypted.aad
    credential.rotated_at = now
    account.last_auth_success_at = now
    account.last_auth_error = None
    logger.info("syncjobs.credentials.token_refreshed", account_id=account.id)
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
