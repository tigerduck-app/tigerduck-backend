"""Credential blob access for the sync worker.

Token-only storage: the blob contains a Moodle wstoken (no NTUST
password). When the token expires or is revoked, the sync job disables
and a push notification asks the user to open the app — the foreground
PATCH /v3/auth/credentials sends a fresh token and auto-revives the job.
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
)
from server.auth.models import (
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
)
from server.syncjobs.models import SyncJob, SyncJobStatus

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


async def mark_credentials_invalid(
    session: AsyncSession,
    *,
    account: ExternalAccount,
    user_id: uuid.UUID,
    error: str,
) -> None:
    """Credential failure handling: flag the account, disable every sync
    job for the user, and queue ONE system push so the user is prompted
    to re-open the app (which sends a fresh Moodle token via
    PATCH /v3/auth/credentials)."""
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
