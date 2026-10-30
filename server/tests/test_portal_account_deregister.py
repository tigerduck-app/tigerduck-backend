"""The portal's account deregister, run against the server's tables. In the
server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.deregister import (  # noqa: E402
    DeregisterRequest,
    deregister_student,
)
from server.auth.models import (  # noqa: E402
    AuthSession,
    DeviceActiveDay,
    User,
    UserDevice,
)
from server.db import Base  # noqa: E402
from server.syncjobs.models import SyncLogEntry  # noqa: E402

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

