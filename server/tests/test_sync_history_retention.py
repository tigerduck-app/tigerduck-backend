"""Pruning the sync log and sync runs, and what the apps' change feed does
once its own history is pruned."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import structlog
from sqlalchemy import select, text, update
from structlog.testing import capture_logs

from server import db
from server.auth.models import ExternalAccount, User
from server.config import Settings
from server.db import build_session_factory
from server.scheduler.runtime import build_scheduler
from server.sync.models import UserChangeLog
from server.sync.retention import purge_expired_changelog
from server.syncjobs.models import SyncJob, SyncLogEntry, SyncRun
from server.syncjobs import retention as sync_retention
from server.syncjobs.retention import prune_sync_history
from server.tests.test_sync_api import bearer, do_login, seed_changes

pytestmark = pytest.mark.asyncio(loop_scope="session")

SETTINGS = Settings(sync_history_retention_days=30, sync_changelog_retention_days=30)
NOW = datetime.now(UTC)
OLD, RECENT = NOW - timedelta(days=31), NOW - timedelta(days=29)


async def _user(session) -> User:
    user = User(student_id="b1")
    session.add(user)
    await session.flush()
    return user


async def _log(session, *entries: tuple[str, datetime]) -> None:
    user = await _user(session)
    session.add_all(
        SyncLogEntry(user_id=user.id, source="s", message=message, ts=ts)
        for message, ts in entries
    )
    await session.commit()


async def test_a_purge_deletes_one_batch_per_transaction(
    db_session, prepared_engine, monkeypatch
) -> None:
    monkeypatch.setattr(db, "PRUNE_BATCH_ROWS", 2)
    await _log(db_session, *[(str(i), OLD) for i in range(5)], ("recent", RECENT))
    batches = []

    async def record(session, rows) -> None:
        batches.append([row.message for row in rows])

    deleted = await db.delete_in_batches(
        build_session_factory(prepared_engine),
        SyncLogEntry,
        SyncLogEntry.ts < NOW - timedelta(days=30),
        returning=[SyncLogEntry.message],
        then=record,
    )

    assert deleted == (5, False)
    assert batches == [["0", "1"], ["2", "3"], ["4"]]


_DEADLOCK = text(
    "DO $$ BEGIN RAISE EXCEPTION 'deadlock detected' USING ERRCODE = '40P01'; END $$"
)


async def test_a_batch_aborted_as_a_deadlock_is_tried_again(
    db_session, prepared_engine
) -> None:
    await _log(db_session, ("old", OLD), ("recent", RECENT))
    tries = []

    async def deadlocked_once(session, rows) -> None:
        tries.append(len(rows))
        if len(tries) == 1:
            await session.execute(_DEADLOCK)

    deleted = await db.delete_in_batches(
        build_session_factory(prepared_engine),
        SyncLogEntry,
        SyncLogEntry.ts < NOW - timedelta(days=30),
        then=deadlocked_once,
    )

    assert (deleted, tries) == ((1, False), [1, 1])
    left = (await db_session.execute(select(SyncLogEntry.message))).scalars().all()
    assert left == ["recent"]


async def test_a_batch_deadlocked_every_time_ends_the_run(
    db_session, prepared_engine, monkeypatch
) -> None:
    await _log(db_session, ("old", OLD))
    tries = []

    async def deadlocked(session, rows) -> None:
        tries.append(len(rows))
        await session.execute(_DEADLOCK)

    # A cached logger keeps an earlier app's processors, out of capture_logs' reach.
    monkeypatch.setattr(db, "logger", structlog.get_logger(db.__name__))
    with capture_logs() as logs:
        deleted = await db.delete_in_batches(
            build_session_factory(prepared_engine),
            SyncLogEntry,
            SyncLogEntry.ts < NOW - timedelta(days=30),
            then=deadlocked,
        )

    assert (deleted, len(tries)) == ((0, True), db.PRUNE_BATCH_ATTEMPTS)
    assert [log["table"] for log in logs if log["event"] == "retention.batch.gave_up"] == [
        "sync_log_entries"
    ]
    left = (await db_session.execute(select(SyncLogEntry.message))).scalars().all()
    assert left == ["old"]


async def test_the_night_ends_when_the_log_purge_stops_early(monkeypatch) -> None:
    purged = []

    async def gave_up(factory, model, *where):
        purged.append(model)
        return 3, True

    monkeypatch.setattr(sync_retention, "delete_in_batches", gave_up)
    monkeypatch.setattr(sync_retention, "logger", structlog.get_logger(sync_retention.__name__))
    with capture_logs() as logs:
        deleted = await prune_sync_history(None, SETTINGS)

    assert (deleted, purged) == (3, [SyncLogEntry])
    assert [log["stopped_early"] for log in logs] == [True]


async def test_log_entries_past_the_window_go(db_session, prepared_engine) -> None:
    await _log(db_session, ("old", OLD), ("recent", RECENT), ("now", NOW))

    deleted = await prune_sync_history(build_session_factory(prepared_engine), SETTINGS)

    assert deleted == 1
    left = (await db_session.execute(select(SyncLogEntry.message))).scalars().all()
    assert sorted(left) == ["now", "recent"]


async def test_finished_runs_past_the_window_go(db_session, prepared_engine) -> None:
    user = await _user(db_session)
    account = ExternalAccount(user_id=user.id, provider="ntust_sso", external_user_id="b1")
    db_session.add(account)
    await db_session.flush()
    job = SyncJob(user_id=user.id, external_account_id=account.id, job_type="ntust_courses")
    db_session.add(job)
    await db_session.flush()
    db_session.add_all(
        [
            SyncRun(sync_job_id=job.id, user_id=user.id, status="succeeded",
                    started_at=OLD, finished_at=OLD, error="old"),
            SyncRun(sync_job_id=job.id, user_id=user.id, status="running",
                    started_at=OLD, error="running"),
            SyncRun(sync_job_id=job.id, user_id=user.id, status="failed",
                    started_at=RECENT, finished_at=RECENT, error="recent"),
        ]
    )
    await db_session.commit()

    deleted = await prune_sync_history(build_session_factory(prepared_engine), SETTINGS)

    assert deleted == 1
    left = (await db_session.execute(select(SyncRun.error))).scalars().all()
    assert sorted(left) == ["recent", "running"]


async def test_a_device_behind_the_pruned_feed_gets_a_full_resync(client) -> None:
    """The sync history is no part of the feed: the feed's own pruning
    answers a cursor it passed with 410, never with a page that skips."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    revisions = await seed_changes(client, login["user"]["id"], 3)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(
            update(UserChangeLog)
            .where(UserChangeLog.revision.in_(revisions[:2]))
            .values(created_at=OLD)
        )
        session.add(SyncLogEntry(user_id=user_id, source="s", message="m", ts=OLD))
        await session.commit()

    await prune_sync_history(factory, SETTINGS)
    await purge_expired_changelog(factory, SETTINGS)

    behind = await client.get(
        f"/v3/sync?since_revision={revisions[0]}", headers=bearer(login)
    )
    assert behind.status_code == 410
    assert behind.json()["full_sync_required"] is True
    caught_up = await client.get(
        f"/v3/sync?since_revision={revisions[1]}", headers=bearer(login)
    )
    assert [c["revision"] for c in caught_up.json()["changes"]] == revisions[2:]
    assert (await client.get("/v3/sync/full", headers=bearer(login))).status_code == 200


async def test_the_nightly_jobs_run_at_their_times_at_school(test_settings) -> None:
    """At wall-clock times: an interval would start over at every deploy."""
    scheduler = build_scheduler(object(), object(), test_settings, llm=object())
    scheduler.start(paused=True)
    try:
        started = datetime.now(UTC)
        runs = {
            job.id: job.next_run_time
            for job in scheduler.get_jobs()
            if job.id.endswith("_retention") or job.id == "device_retirement"
        }
    finally:
        scheduler.shutdown(wait=False)
    local = {
        job_id: at.astimezone(ZoneInfo("Asia/Taipei")).time().isoformat()
        for job_id, at in runs.items()
    }
    assert local == {
        "bulletin_retention": "03:00:00",
        "sync_changelog_retention": "03:05:00",
        "push_job_retention": "03:10:00",
        "admin_audit_retention": "03:15:00",
        "sync_history_retention": "03:20:00",
        "auth_session_retention": "03:25:00",
        "device_retirement": "04:00:00",
    }
    assert all(timedelta(0) < at - started <= timedelta(days=1) for at in runs.values())
