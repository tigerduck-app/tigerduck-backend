"""Credential blob handling for the sync worker, incl. the iron rule."""

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
    refresh_moodle_token,
)
from server.syncjobs.models import SyncJob
from server.syncjobs.moodle_client import (
    ObtainedToken,
    SsoAuthFailed,
    SsoUnavailable,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cipher() -> CredentialCipher:
    return CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )


async def _setup(session, cipher, *, password_verified=False):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()
    blob = cipher.encrypt(
        {
            "ntust_password": "secret-pw",
            "password_verified": password_verified,
            "token_cache": {"moodle_token": "old-tok"},
        },
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


class StaticObtainer:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = 0

    async def obtain_token(self, *, username, password):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


async def test_load_credential_blob_roundtrip(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    loaded_account, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    assert loaded_account.id == account.id
    assert blob["ntust_password"] == "secret-pw"
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


async def test_refresh_token_success_marks_password_verified(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher, password_verified=False)
    obtainer = StaticObtainer(
        result=ObtainedToken(token="fresh-tok", private_token="fresh-priv")
    )
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )

    token = await refresh_moodle_token(
        db_session, cipher, obtainer, account=account, blob=blob
    )
    await db_session.commit()
    assert token == "fresh-tok"
    assert obtainer.calls == 1

    _, blob2 = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    assert blob2["password_verified"] is True
    assert blob2["token_cache"]["moodle_token"] == "fresh-tok"
    assert blob2["token_cache"]["moodle_private_token"] == "fresh-priv"
    assert blob2["ntust_password"] == "secret-pw"


async def test_refresh_token_auth_failure_raises_credential_invalid(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    obtainer = StaticObtainer(error=SsoAuthFailed("invalidlogin"))
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    with pytest.raises(CredentialInvalid):
        await refresh_moodle_token(
            db_session, cipher, obtainer, account=account, blob=blob
        )
    assert obtainer.calls == 1  # exactly one SSO attempt, never more


async def test_refresh_token_network_failure_propagates(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    obtainer = StaticObtainer(error=SsoUnavailable("timeout"))
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    with pytest.raises(SsoUnavailable):
        await refresh_moodle_token(
            db_session, cipher, obtainer, account=account, blob=blob
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
