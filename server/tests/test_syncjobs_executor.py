"""Executor: stale-lock recovery, claiming, global cap, execution paths."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
)
from server.db import build_session_factory
from server.sync.models import UserAssignment, UserChangeLog
from server.syncjobs.executor import SyncWorker, run_sync_tick
from server.syncjobs.models import SyncJob, SyncPolicy, SyncRun
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    ObtainedToken,
    SsoAuthFailed,
    SsoUnavailable,
)
from server.syncjobs.policies import ensure_default_policies

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cipher() -> CredentialCipher:
    return CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )


class StubFetcher:
    def __init__(self, results=None, errors=()):
        self.results = results if results is not None else []
        self.errors = list(errors)
        self.calls: list[str] = []

    async def fetch_assignments(self, *, token):
        self.calls.append(token)
        if self.errors:
            raise self.errors.pop(0)
        return self.results


class StubObtainer:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def obtain_token(self, *, username, password):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _worker(
    prepared_engine, test_settings, fetcher=None, obtainer=None
) -> SyncWorker:
    return SyncWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        cipher=_cipher(),
        fetcher=fetcher if fetcher is not None else StubFetcher(),
        token_obtainer=obtainer if obtainer is not None else StubObtainer(),
        worker_id="test-worker",
    )


async def _setup_user_job(
    session,
    *,
    student_id="b11203058",
    token="tok-1",
    password_verified=True,
    job_kwargs=None,
):
    cipher = _cipher()
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=student_id
    )
    session.add(account)
    await session.flush()
    blob = cipher.encrypt(
        {
            "ntust_password": "pw",
            "password_verified": password_verified,
            "token_cache": {"moodle_token": token},
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
        **(job_kwargs or {}),
    )
    session.add(job)
    await ensure_default_policies(session)
    await session.commit()
    return user, account, job


def _fa(aid: int) -> FetchedAssignment:
    return FetchedAssignment(
        moodle_course_id=7001,
        moodle_assignment_id=aid,
        course_name="資料結構",
        title=f"HW{aid}",
        due_at=datetime.now(UTC) + timedelta(days=7),
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=None,
        intro_html=None,
    )


async def test_stale_running_job_recovered(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(
        db_session,
        job_kwargs={
            "status": "running",
            "locked_by": "dead-worker",
            "locked_at": datetime.now(UTC) - timedelta(minutes=11),
        },
    )
    run = SyncRun(sync_job_id=job.id, user_id=user.id)
    db_session.add(run)
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    await db_session.refresh(job)
    # Recovered to pending, then immediately claimed+executed this tick.
    assert job.status == "pending"
    assert job.last_success_at is not None
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error == "stale_lock_recovered"


async def test_fresh_running_job_not_recovered_and_counts_against_cap(
    db_session, prepared_engine, test_settings
):
    await _setup_user_job(
        db_session,
        job_kwargs={
            "status": "running",
            "locked_by": "other-worker",
            "locked_at": datetime.now(UTC) - timedelta(minutes=1),
        },
    )
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []  # nothing claimable


async def test_global_concurrency_cap_limits_claims(
    db_session, prepared_engine, test_settings
):
    # 5 fresh running rows (other workers) → cap 5 reached → claim nothing.
    for i in range(5):
        await _setup_user_job(
            db_session,
            student_id=f"b1120300{i}",
            job_kwargs={
                "status": "running",
                "locked_by": "other",
                "locked_at": datetime.now(UTC),
            },
        )
    await _setup_user_job(db_session, student_id="b11203099")

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []


async def test_disabled_policy_jobs_not_claimed(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    policy = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    policy.enabled = False
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    assert fetcher.calls == []
    await db_session.refresh(job)
    assert job.status == "pending"


async def test_policy_active_window_respected(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    policy = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    policy.active_from = datetime.now(UTC) + timedelta(days=1)
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []
