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


class StubCourseFetcher:
    def __init__(self, results=None, errors=()):
        self.results = results if results is not None else []
        self.errors = list(errors)

    async def fetch_courses(self, *, token):
        if self.errors:
            raise self.errors.pop(0)
        return self.results


def _worker(
    prepared_engine, test_settings, fetcher=None, course_fetcher=None
) -> SyncWorker:
    return SyncWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        cipher=_cipher(),
        fetcher=fetcher if fetcher is not None else StubFetcher(),
        course_fetcher=course_fetcher if course_fetcher is not None else StubCourseFetcher(),
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


async def test_successful_run_upserts_and_reschedules(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(results=[_fa(1), _fa(2)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    executed = await run_sync_tick(worker)
    assert executed == 1
    assert fetcher.calls == ["tok-1"]

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_success_at is not None
    assert job.last_error is None
    # rescheduled ~8h out (policy interval 28800s)
    assert job.run_after > datetime.now(UTC) + timedelta(hours=7)

    rows = (
        (
            await db_session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.fetched_count == 2
    assert run.changed_count == 2
    entries = (
        (
            await db_session.execute(
                select(UserChangeLog).where(UserChangeLog.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 2


async def test_expired_token_disables_job_and_queues_push(
    db_session, prepared_engine, test_settings
):
    """Token-only: an expired Moodle token disables the sync job and
    queues a push notification. No password-based refresh — the user must
    open the app to send a fresh token via PATCH /auth/credentials."""
    user, account, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(errors=[MoodleTokenInvalid("dead")])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "disabled"
    assert job.last_error == "credential_invalid"
    await db_session.refresh(account)
    assert account.credential_status == "invalid"
    push = (
        await db_session.execute(
            select(PushJob).where(PushJob.user_id == user.id)
        )
    ).scalar_one()
    assert push.scenario == "reauth_required"


async def test_network_failure_backs_off_then_terminal_after_max_attempts(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(
        errors=[
            MoodleUnreachable("net"),
            MoodleUnreachable("net"),
            MoodleUnreachable("net"),
        ]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1
    assert job.last_error.startswith("sync_failed")
    assert job.run_after > datetime.now(UTC)  # backoff in the future

    # Force-due and run twice more → terminal failed at max_attempts=3.
    for _ in range(2):
        job.run_after = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_sync_tick(worker)
        await db_session.refresh(job)
    assert job.status == "failed"
    assert job.attempts == 3


async def test_rate_limited_records_school_rate_limited(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(errors=[MoodleRateLimited("429")])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "school_rate_limited"


