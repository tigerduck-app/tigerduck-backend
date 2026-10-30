"""The portal's account deregister, run against the server's tables. In the
server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import Table, text
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



async def _waiting_on_a_lock(task: asyncio.Task, factory) -> None:
    async with asyncio.timeout(10), factory() as session:
        while not task.done():
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            # pg_stat_activity holds still for the length of a transaction.
            await session.rollback()
            if waiting:
                return
            await asyncio.sleep(0.01)


async def _old_rows(db_session) -> None:
    """An account with a session and a sync log row past their windows."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(user_id=user.id, client_device_id="iphone-1", platform="ios")
    db_session.add(phone)
    await db_session.flush()
    gone = datetime.now(UTC) - timedelta(days=31)
    db_session.add(
        AuthSession(
            user_id=user.id,
            device_id=phone.id,
            refresh_token_hash="h",
            issued_at=gone - timedelta(days=90),
            expires_at=gone,
        )
    )
    db_session.add(SyncLogEntry(user_id=user.id, source="s", message="m", ts=gone))
    await db_session.commit()


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
        deregistering.append(
            asyncio.create_task(
                deregister_student(DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool)
            )
        )
        await _waiting_on_a_lock(deregistering[0], factory)
        await keep_active_days(session, rows)

    monkeypatch.setattr(retention, "_keep_active_days", racing)

    assert await retention.prune_expired_sessions(factory, Settings()) == 1
    assert (await deregistering[0]).status_code == 200
    assert await portal_pool.fetchval("SELECT count(*) FROM user_devices") == 0


async def test_the_purges_during_a_deregister_wait_for_it(
    db_session, portal_pool, prepared_engine, monkeypatch
):
    await _old_rows(db_session)
    factory = build_session_factory(prepared_engine)
    audit = deregister_route.audit
    purging: list[asyncio.Task] = []

    async def purge() -> tuple[int, int]:
        return (
            await prune_sync_history(factory, Settings()),
            await retention.prune_expired_sessions(factory, Settings()),
        )

    async def racing(conn, request, action, **detail) -> None:
        purging.append(asyncio.create_task(purge()))
        await _waiting_on_a_lock(purging[0], factory)
        await audit(conn, request, action, **detail)

    monkeypatch.setattr(deregister_route, "audit", racing)

    result = await deregister_student(DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool)

    assert result.status_code == 200
    assert await purging[0] == (0, 0)


async def test_a_deregister_takes_the_account_before_its_rows(
    db_session, portal_pool, prepared_engine
):
    """A purge batch holds the account and then deletes its rows. The
    deregister had deleted the sync runs with the NTUST account first, then
    waited for the account: the two deadlocked."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    account = ExternalAccount(user_id=user.id, provider="ntust_sso", external_user_id="b1")
    db_session.add(account)
    await db_session.flush()
    job = SyncJob(user_id=user.id, external_account_id=account.id, job_type="ntust_courses")
    db_session.add(job)
    await db_session.flush()
    db_session.add(SyncRun(sync_job_id=job.id, user_id=user.id, status="succeeded"))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    async with portal_pool.acquire() as batch, batch.transaction():
        await batch.execute("SELECT 1 FROM users WHERE id = $1 FOR KEY SHARE", user.id)
        deregistering = asyncio.create_task(
            deregister_student(DeregisterRequest(student_id="b1"), _REQUEST, pool=portal_pool)
        )
        await _waiting_on_a_lock(deregistering, factory)
        await batch.execute("DELETE FROM sync_runs WHERE user_id = $1", user.id)

    assert (await deregistering).status_code == 200
