"""Roundtrip + constraint tests for the Phase-3 sync infrastructure tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.auth.models import ExternalAccount, User
from server.syncjobs.models import (
    SyncJob,
    SyncJobStatus,
    SyncJobType,
    SyncPolicy,
    SyncRun,
    SyncRunStatus,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user_account(session) -> tuple[User, ExternalAccount]:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()
    return user, account


async def test_sync_policy_roundtrip_defaults(db_session):
    policy = SyncPolicy(
        job_type=SyncJobType.moodle_assignments.value,
        default_interval_seconds=28800,
    )
    db_session.add(policy)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    assert row.enabled is True
    assert row.priority == 100
    assert row.max_attempts == 3
    assert row.active_from is None and row.active_until is None


async def test_sync_policy_rejects_unknown_job_type(db_session):
    db_session.add(SyncPolicy(job_type="bogus", default_interval_seconds=60))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_policy_job_type_unique(db_session):
    db_session.add(
        SyncPolicy(job_type="calendar", default_interval_seconds=604800)
    )
    await db_session.commit()
    db_session.add(
        SyncPolicy(job_type="calendar", default_interval_seconds=60)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_job_roundtrip_and_unique_per_user_type(db_session):
    user, account = await _make_user_account(db_session)
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type=SyncJobType.moodle_assignments.value,
    )
    db_session.add(job)
    await db_session.commit()

    assert job.status == SyncJobStatus.pending.value
    assert job.priority == 100
    assert job.attempts == 0
    assert job.cursor == {}
    assert job.run_after is not None

    db_session.add(
        SyncJob(
            user_id=user.id,
            external_account_id=account.id,
            job_type=SyncJobType.moodle_assignments.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_job_rejects_bad_status(db_session):
    user, account = await _make_user_account(db_session)
    db_session.add(
        SyncJob(
            user_id=user.id,
            external_account_id=account.id,
            job_type="moodle_assignments",
            status="exploded",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_run_roundtrip(db_session):
    user, account = await _make_user_account(db_session)
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
    )
    db_session.add(job)
    await db_session.flush()

    run = SyncRun(sync_job_id=job.id, user_id=user.id)
    db_session.add(run)
    await db_session.commit()

    assert run.status == SyncRunStatus.running.value
    assert run.started_at is not None
    assert run.metadata_json == {}

    run.status = SyncRunStatus.succeeded.value
    run.finished_at = datetime.now(UTC) + timedelta(seconds=1)
    run.fetched_count = 12
    run.changed_count = 3
    await db_session.commit()
