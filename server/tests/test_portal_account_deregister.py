"""The portal's account deregister, run against the server's tables. In the
server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import Table, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes import deregister as deregister_route  # noqa: E402
from portal.app.routes.deregister import (  # noqa: E402
    DeregisterRequest,
    deregister_student,
)
from server.auth.models import (  # noqa: E402
    AuthSession,
    DeviceActiveDay,
    ExternalAccount,
    User,
    UserDevice,
)
from server.auth import retention  # noqa: E402
from server.config import Settings  # noqa: E402
from server.db import Base, build_session_factory  # noqa: E402
from server.syncjobs.models import SyncJob, SyncLogEntry, SyncRun  # noqa: E402
from server.syncjobs.retention import prune_sync_history  # noqa: E402
from server.tests.test_auth_refresh import do_login  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REQUEST = SimpleNamespace(state=SimpleNamespace(actor="ops@example.com"))

# The operator's record, which names accounts by id only and outlives them.
_KEPT = {"admin_audit_log"}


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


def _account_columns(table: Table):
    """The columns that can hold a user's or a device's id, with a foreign
    key or without one."""
    for column in table.columns:
        targets = {fk.column.table.name for fk in column.foreign_keys}
        named = isinstance(column.type, UUID) and column.name.endswith(
            ("user_id", "device_id")
        )
        if targets & {"users", "user_devices"} or named:
            yield column


async def test_deregister_leaves_no_row_of_the_account(db_session, portal_pool):
    """tigerduck-backend#90: the sync log has no foreign key to users and
    outlived the account."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(user_id=user.id, client_device_id="iphone-1", platform="ios")
    db_session.add(phone)
    await db_session.flush()
    db_session.add_all(
        [
            AuthSession(
                user_id=user.id,
                device_id=phone.id,
                refresh_token_hash="h",
                expires_at=datetime.now(UTC) + timedelta(days=30),
            ),
            SyncLogEntry(
                user_id=user.id, device_id=phone.id, source="executor", message="m"
            ),
            DeviceActiveDay(
                device_id=phone.id,
                day=date(2026, 6, 1),
                first_at=datetime(2026, 6, 1, tzinfo=UTC),
                last_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        ]
    )
    await db_session.commit()
    ids = [user.id, phone.id]

    result = await deregister_student(
        DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool
    )

    assert result.status_code == 200
    left = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _KEPT:
            continue
        for column in _account_columns(table):
            count = await portal_pool.fetchval(
                f"SELECT count(*) FROM {table.name} WHERE {column.name} = ANY($1::uuid[])",
                ids,
            )
            if count:
                left[f"{table.name}.{column.name}"] = count
    assert left == {}


async def _blocked_by(task: asyncio.Task, factory, pid: int) -> str:
    """Wait until a backend waits on a lock the backend `pid` holds, and
    return the statement it waits in. `task` ending first fails the test:
    the race it sets up never happened."""
    async with asyncio.timeout(10), factory() as session:
        while not task.done():
            waiting = await session.scalar(
                text(
                    "SELECT query FROM pg_stat_activity"
                    " WHERE :pid = ANY(pg_blocking_pids(pid)) LIMIT 1"
                ),
                {"pid": pid},
            )
            # pg_stat_activity holds still for the length of a transaction.
            await session.rollback()
            if waiting is not None:
                return waiting
            await asyncio.sleep(0.01)
    raise AssertionError(f"{task!r} ended without waiting on backend {pid}")


async def _old_rows(db_session) -> User:
    """An account with an NTUST account, and a session, a sync log row and a
    sync run past their windows."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    account = ExternalAccount(user_id=user.id, provider="ntust_sso", external_user_id="b1")
    phone = UserDevice(user_id=user.id, client_device_id="iphone-1", platform="ios")
    db_session.add_all([account, phone])
    await db_session.flush()
    job = SyncJob(user_id=user.id, external_account_id=account.id, job_type="ntust_courses")
    db_session.add(job)
    await db_session.flush()
    gone = datetime.now(UTC) - timedelta(days=31)
    db_session.add_all(
        [
            AuthSession(
                user_id=user.id,
                device_id=phone.id,
                refresh_token_hash="h",
                issued_at=gone - timedelta(days=90),
                expires_at=gone,
            ),
            SyncLogEntry(user_id=user.id, source="s", message="m", ts=gone),
            SyncRun(sync_job_id=job.id, user_id=user.id, status="succeeded",
                    started_at=gone, finished_at=gone),
        ]
    )
    await db_session.commit()
    return user


def _deregister(portal_pool) -> asyncio.Task:
    return asyncio.create_task(
        deregister_student(DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool)
    )


async def test_a_deregister_during_a_session_purge_waits_for_it(
    db_session, portal_pool, prepared_engine, monkeypatch
):
    """The purge had deleted the account's sessions and then took a lock on
    its device, which the deregister already held while it waited on those
    sessions: one of the two was aborted as a deadlock."""
    await _old_rows(db_session)
    factory = build_session_factory(prepared_engine)
    keep_active_days = retention._keep_active_days
    deregistering: list[asyncio.Task] = []

    async def racing(session, rows) -> None:
        deregistering.append(_deregister(portal_pool))
        purge_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        await _blocked_by(deregistering[0], factory, purge_pid)
        await keep_active_days(session, rows)

    monkeypatch.setattr(retention, "_keep_active_days", racing)

    assert await retention.prune_expired_sessions(factory, Settings()) == 1
    assert (await deregistering[0]).status_code == 200
    assert await portal_pool.fetchval("SELECT count(*) FROM user_devices") == 0


async def test_the_purges_during_a_deregister_wait_at_the_account(
    db_session, portal_pool, prepared_engine, monkeypatch
):
    """Each purge batch waits for the account before it locks any row."""
    await _old_rows(db_session)
    factory = build_session_factory(prepared_engine)
    audit = deregister_route.audit
    purging: list[asyncio.Task] = []
    waited_in: list[str] = []

    async def purge() -> tuple[int, int]:
        return (
            await prune_sync_history(factory, Settings()),
            await retention.prune_expired_sessions(factory, Settings()),
        )

    async def racing(conn, request, action, **detail) -> None:
        purging.append(asyncio.create_task(purge()))
        deregister_pid = await conn.fetchval("SELECT pg_backend_pid()")
        waited_in.append(await _blocked_by(purging[0], factory, deregister_pid))
        await audit(conn, request, action, **detail)

    monkeypatch.setattr(deregister_route, "audit", racing)

    result = await deregister_student(DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool)

    assert result.status_code == 200
    assert "FOR KEY SHARE" in waited_in[0]
    assert await purging[0] == (0, 0)


async def test_a_deregister_during_a_purge_batch_locks_no_row_first(
    db_session, portal_pool, prepared_engine
):
    """A purge batch holds the account and then deletes its rows. The
    deregister had deleted the sync runs with the NTUST account first, then
    waited for the account: the two deadlocked."""
    user = await _old_rows(db_session)
    factory = build_session_factory(prepared_engine)

    async with portal_pool.acquire() as batch, batch.transaction():
        await batch.execute("SELECT 1 FROM users WHERE id = $1 FOR KEY SHARE", user.id)
        deregistering = _deregister(portal_pool)
        await _blocked_by(deregistering, factory, await batch.fetchval("SELECT pg_backend_pid()"))
        await batch.execute("DELETE FROM sync_runs WHERE user_id = $1", user.id)

    assert (await deregistering).status_code == 200


async def test_a_deregister_during_a_sign_in_waits_for_it(
    db_session, portal_pool, prepared_engine
):
    """A sign-in locks the NTUST account (`lock_accounts`) and then adds a
    session, which needs the users row. The deregister had locked that row
    first and then waited for the NTUST account: the two deadlocked."""
    user = await _old_rows(db_session)
    factory = build_session_factory(prepared_engine)

    async with portal_pool.acquire() as sign_in, sign_in.transaction():
        await sign_in.execute(
            "SELECT 1 FROM external_accounts WHERE user_id = $1 ORDER BY id FOR UPDATE",
            user.id,
        )
        deregistering = _deregister(portal_pool)
        await _blocked_by(deregistering, factory, await sign_in.fetchval("SELECT pg_backend_pid()"))
        await sign_in.execute(
            "INSERT INTO auth_sessions (user_id, refresh_token_hash, expires_at)"
            " VALUES ($1, 'new', now() + interval '90 days')",
            user.id,
        )

    assert (await deregistering).status_code == 200
    assert await portal_pool.fetchval("SELECT count(*) FROM auth_sessions") == 0


async def test_a_deregister_deadlocked_with_the_sync_executor_is_tried_again(
    db_session, portal_pool, prepared_engine, caplog
):
    """The executor locks a job and then needs the users row for its run; the
    deregister holds that row and waits for the job, which its delete of the
    NTUST account cascades to. Postgres aborts the deregister, and the retry
    reports what it deleted, the executor's log row too."""
    user = await _old_rows(db_session)
    job_id = await db_session.scalar(select(SyncJob.id).where(SyncJob.user_id == user.id))
    factory = build_session_factory(prepared_engine)
    caplog.set_level(logging.WARNING, logger=deregister_route.__name__)

    async with portal_pool.acquire() as executor, executor.transaction():
        # So that the deregister, waiting first, is the one Postgres aborts.
        await executor.execute("SET LOCAL deadlock_timeout = '30s'")
        await executor.execute("SELECT 1 FROM sync_jobs WHERE id = $1 FOR UPDATE", job_id)
        deregistering = _deregister(portal_pool)
        await _blocked_by(deregistering, factory, await executor.fetchval("SELECT pg_backend_pid()"))
        await executor.execute(
            "INSERT INTO sync_runs (sync_job_id, user_id) VALUES ($1, $2)", job_id, user.id
        )
        await executor.execute(
            "INSERT INTO sync_log_entries (user_id, source, message) VALUES ($1, 's', 'm')",
            user.id,
        )

    response = await deregistering
    assert response.status_code == 200
    assert json.loads(response.body)["deleted_counts"]["sync_log_entries"] == 2
    assert "deadlock" in caplog.text
    assert await portal_pool.fetchval("SELECT count(*) FROM sync_runs") == 0


async def test_a_sign_in_writes_the_users_row_after_the_account_lock(
    client, prepared_engine
):
    """Reactivating a deleted account, the sign-in wrote the users row before
    it took the NTUST account, the other way round from a deregister."""
    user_id = uuid.UUID((await do_login(client))["user"]["id"])
    factory = build_session_factory(prepared_engine)
    async with factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(deleted_at=func.now()))
        await session.commit()

    async with prepared_engine.connect() as account_lock:
        await account_lock.execute(
            text("SELECT 1 FROM external_accounts WHERE user_id = :u FOR UPDATE"), {"u": user_id}
        )
        signing_in = asyncio.create_task(do_login(client))
        await _blocked_by(
            signing_in, factory, await account_lock.scalar(text("SELECT pg_backend_pid()"))
        )
        await account_lock.execute(
            text("SELECT 1 FROM users WHERE id = :u FOR UPDATE NOWAIT"), {"u": user_id}
        )
        await account_lock.rollback()

    await signing_in

