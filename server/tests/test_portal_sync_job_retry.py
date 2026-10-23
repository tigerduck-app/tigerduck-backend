"""The portal's two retry buttons, run against the database the executor
leaves behind.

A job whose last run failed on the school's side stays pending and waits
for its policy interval (server/syncjobs/executor.py), so a button that
picks jobs by status alone would pass over it. Lives in the server suite
for the reason test_calendar_gaps.py gives: the portal has no test runner
of its own.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.moodle.jobs import (  # noqa: E402
    RetryAllRequest,
    retry_all_jobs,
    retry_all_moodle,
)
from server.auth.models import ExternalAccount, User, UserDevice  # noqa: E402
from server.syncjobs.models import SyncJob  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

_LATER = timedelta(hours=8)
_REQUEST = SimpleNamespace(state=SimpleNamespace(actor="ops@example.com"))


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


async def _job(db_session, student_id: str, **fields) -> SyncJob:
    user = User(student_id=student_id)
    db_session.add(user)
    await db_session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=student_id
    )
    db_session.add(account)
    await db_session.flush()
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
        **fields,
    )
    db_session.add(job)
    await db_session.commit()
    return job


async def _queue(db_session) -> dict[str, SyncJob]:
    """One job in each state the retry buttons have to tell apart."""
    later = datetime.now(UTC) + _LATER
    return {
        "failed": await _job(
            db_session, "b1", status="failed", attempts=3,
            last_error="sync_failed:RuntimeError", run_after=later,
        ),
        "disabled": await _job(
            db_session, "b2", status="disabled", last_error="credential_invalid",
            run_after=later,
        ),
        "retrying": await _job(
            db_session, "b3", status="pending", attempts=0,
            last_error="sync_failed:http_503", run_after=later,
        ),
        "healthy": await _job(db_session, "b4", status="pending", run_after=later),
        "running": await _job(
            db_session, "b5", status="running", locked_by="worker",
            locked_at=datetime.now(UTC), run_after=later,
        ),
    }


async def _requeued(db_session, jobs: dict[str, SyncJob]) -> set[str]:
    due = datetime.now(UTC) + timedelta(minutes=1)
    result = set()
    for name, job in jobs.items():
        row = (
            await db_session.execute(
                select(SyncJob)
                .where(SyncJob.id == job.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if row.run_after < due:
            assert (row.status, row.attempts, row.last_error) == ("pending", 0, None)
            result.add(name)
    return result


async def test_retry_all_failed_takes_a_job_that_keeps_retrying(
    db_session, portal_pool
):
    """After an outage the jobs it caught wait for their interval, hours
    away. The button an operator presses once the school is back starts
    them now, with the failed and disabled ones, and leaves healthy and
    running jobs alone."""
    jobs = await _queue(db_session)

    result = await retry_all_moodle(_REQUEST, RetryAllRequest(), pool=portal_pool)

    assert result == {"retried": 3, "notified": 0}
    assert await _requeued(db_session, jobs) == {"failed", "disabled", "retrying"}
    audit = await portal_pool.fetchrow("SELECT actor, action, detail FROM admin_audit_log")
    assert (audit["actor"], audit["action"]) == ("ops@example.com", "sync.retry_failed")
    assert json.loads(audit["detail"]) == {"notify": False, "retried": 3, "notified": 0}


async def test_retry_all_takes_every_job_but_a_running_one(db_session, portal_pool):
    """Retry all asked for status 'active', which no job has (the portal's
    status card calls pending jobs active), so it retried only what Retry
    all failed did, and from now on less."""
    jobs = await _queue(db_session)

    result = await retry_all_jobs(_REQUEST, RetryAllRequest(), pool=portal_pool)

    assert result == {"retried": 4, "notified": 0}
    assert await _requeued(db_session, jobs) == {
        "failed", "disabled", "retrying", "healthy",
    }


@pytest.mark.parametrize("button", [retry_all_moodle, retry_all_jobs])
async def test_the_expire_notification_goes_only_to_a_student_waiting_to_sign_in(
    db_session, portal_pool, button
):
    """tigerduck-backend#54: the notification went to every user whose job
    was pending with no attempts after the retry, a healthy one included.
    A student with no iPhone or iPad syncing has nothing to receive it."""
    jobs = await _queue(db_session)
    await _job(db_session, "b6", status="disabled", last_error="credential_invalid")
    db_session.add(
        UserDevice(user_id=jobs["disabled"].user_id, client_device_id="iphone-1", platform="ios")
    )
    await db_session.commit()

    result = await button(_REQUEST, RetryAllRequest(notify=True), pool=portal_pool)

    assert result["notified"] == 1
    notified = await portal_pool.fetch(
        "SELECT u.student_id, p.scenario FROM push_jobs p JOIN users u ON u.id = p.user_id"
    )
    assert [tuple(r) for r in notified] == [("b2", "reauth_required")]
