"""Credential blob handling for the sync worker (token-only, no password)."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
)
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential_blob,
    mark_credentials_invalid,
)
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cipher() -> CredentialCipher:
    return CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )


async def _setup(session, cipher):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()
    blob = cipher.encrypt(
        {"token_cache": {"moodle_token": "old-tok"}},
        build_credential_aad(account.id, "ntust_sso"),
    )
    session.add(
        ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id=blob.key_id,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            aad=blob.aad,
        )
    )
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
    )
    session.add(job)
    await session.commit()
    return user, account, job


async def test_load_credential_blob_roundtrip(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    loaded_account, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    assert loaded_account.id == account.id
    assert blob["token_cache"]["moodle_token"] == "old-tok"


async def test_load_rejects_non_active_credential_status(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    account.credential_status = "invalid"
    await db_session.commit()
    with pytest.raises(CredentialInvalid):
        await load_credential_blob(
            db_session, cipher, external_account_id=account.id
        )


async def test_mark_credentials_invalid_disables_and_notifies(db_session):
    cipher = _cipher()
    user, account, job = await _setup(db_session, cipher)
    await mark_credentials_invalid(
        db_session,
        account=account,
        user_id=user.id,
        error="credential_invalid",
    )
    await db_session.commit()

    assert account.credential_status == "invalid"
    assert account.last_auth_error == "credential_invalid"
    refreshed_job = await db_session.get(SyncJob, job.id)
    await db_session.refresh(refreshed_job)
    assert refreshed_job.status == "disabled"

    push = (
        await db_session.execute(
            select(PushJob).where(PushJob.user_id == user.id)
        )
    ).scalar_one()
    assert push.channel == "system"
    assert push.scenario == "reauth_required"
    assert push.dedupe_key == f"system:account:{account.id}:reauth_required"

    # Idempotent: second invalidation does not duplicate the push job.
    await mark_credentials_invalid(
        db_session, account=account, user_id=user.id, error="credential_invalid"
    )
    await db_session.commit()
    pushes = (
        (
            await db_session.execute(
                select(PushJob).where(PushJob.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(pushes) == 1
