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
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.crypto import (
    CredentialCipher,
    CredentialCipherError,
    EncryptedBlob,
)
from server.auth.models import (
    APPLE_HANDHELD_PLATFORMS,
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    UserDevice,
)
from server.push.notification_copy import REAUTH_SCENARIO
from server.syncjobs.models import SyncJob, SyncJobStatus

logger = structlog.get_logger(__name__)

# Devices this job targets today: iOS/iPadOS, per spec §4.5. This is the
# "is there anyone worth telling" gate, not a platform filter on delivery —
# macOS is kept out of every `push_jobs` delivery, this one included, by
# the macOS skip in `push.pipeline._materialize`, the one place that
# pipeline makes the decision. (The legacy bulletin and custom-push
# dispatchers, which serve devices not linked to an account, never pass
# through it.)
#
# Two things follow, and the second is a known gap:
#
# 1. Delivery is deliberately broader than this gate. Once a job exists it
#    reaches every non-macOS device on the account, Android included. Spec
#    §4.5 said Apple-only, but argued it from a capability claim that is
#    false -- shipped 2.0.2 Android re-sends a fresh Moodle token on every
#    foreground PATCH /v3/auth/credentials, verified at c04f4705 -- so the
#    broader delivery stands and the spec's restriction does not.
# 2. The gate itself is still Apple-only, so an account holding ONLY Android
#    devices never gets this push at all, even though those devices could
#    act on it. Pointing this at a wider set is the entire fix, but it
#    changes who gets notified, so it wants its own decision rather than
#    riding along with a comment correction.
_REAUTH_CAPABLE_PLATFORMS = APPLE_HANDHELD_PLATFORMS


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

    # Spec §4.5: nobody to tell, nothing to queue. The message asks the user
    # to reopen the app and re-authenticate, which only an iPhone or iPad
    # with course sync on can act on — a job with no such device would
    # fan out to zero eligible recipients and settle as `no_active_tokens`,
    # which reads in the portal as a delivery failure rather than as the
    # deliberate silence it is.
    has_eligible_device = (
        await session.execute(
            select(UserDevice.id)
            .where(
                UserDevice.user_id == user_id,
                UserDevice.deleted_at.is_(None),
                UserDevice.cloud_sync_enabled.is_(True),
                UserDevice.platform.in_(_REAUTH_CAPABLE_PLATFORMS),
            )
            .limit(1)
        )
    ).first() is not None

    if has_eligible_device:
        # Copy is NOT resolved here. One job fans out to every device on the
        # account, and those devices can be in different languages, so the
        # title/body are built per recipient in `push.pipeline._send_one`
        # off that device's `locale`. The payload carries only what is
        # language-independent.
        await session.execute(
            pg_insert(PushJob)
            .values(
                user_id=user_id,
                dedupe_key=f"system:account:{account.id}:{REAUTH_SCENARIO}",
                channel="system",
                scenario=REAUTH_SCENARIO,
                fire_at=now,
                payload={"kind": REAUTH_SCENARIO, "provider": account.provider},
            )
            .on_conflict_do_nothing()
        )
    else:
        logger.info(
            "syncjobs.credentials.reauth_push_skipped",
            account_id=account.id,
            user_id=str(user_id),
            reason="no_reauth_capable_device",
        )
    logger.warning(
        "syncjobs.credentials.invalidated",
        account_id=account.id,
        user_id=str(user_id),
        error=error,
    )
