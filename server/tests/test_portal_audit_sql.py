"""The portal's audit writes (`portal/app/audit.py`) against the real table.

The portal's own tests run without a database, so its SQL is checked here,
where the schema the backend migrates is at hand.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app import audit as portal_audit  # noqa: E402
from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.deregister import (  # noqa: E402
    DeregisterRequest,
    deregister_student,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _request(actor: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(actor=actor))


@pytest_asyncio.fixture(loop_scope="session")
async def pool(db_session, test_settings):
    # db_session resets the schema; the portal speaks plain asyncpg.
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    try:
        yield p
    finally:
        await p.close()


async def test_an_action_row_names_the_actor_targets_and_counts(pool) -> None:
    user_id, device_id = uuid.uuid4(), uuid.uuid4()

    await portal_audit.audit(
        pool, _request("ops@example.com"), "device.deregister",
        user_id=user_id, device_id=str(device_id), count=2,
    )

    row = await pool.fetchrow("SELECT * FROM admin_audit_log")
    assert row["actor"] == "ops@example.com"
    assert row["action"] == "device.deregister"
    assert row["target_user_id"] == user_id
    assert row["target_device_id"] == device_id
    assert json.loads(row["detail"]) == {"count": 2}
    assert row["ip"] is None


async def test_a_view_is_recorded_once_per_actor_and_account_per_10_minutes(
    pool,
) -> None:
    user_id = uuid.uuid4()

    for _ in range(3):
        await portal_audit.audit(
            pool, _request("a@example.com"), "user.view", user_id=user_id
        )
    await portal_audit.audit(
        pool, _request("b@example.com"), "user.view", user_id=user_id
    )
    assert await pool.fetchval("SELECT count(*) FROM admin_audit_log") == 2

    await pool.execute("UPDATE admin_audit_log SET ts = now() - interval '11 minutes'")
    await portal_audit.audit(
        pool, _request("a@example.com"), "user.view", user_id=user_id
    )
    assert await pool.fetchval("SELECT count(*) FROM admin_audit_log") == 3



async def test_a_deregistered_student_leaves_a_record_without_the_student_id(
    pool,
) -> None:
    user_id = await pool.fetchval(
        "INSERT INTO users (student_id) VALUES ('B10000001') RETURNING id"
    )

    r = await deregister_student(
        DeregisterRequest(student_id="B10000001"), _request("ops@example.com"), pool=pool
    )

    assert r.status_code == 200
    row = await pool.fetchrow(
        "SELECT action, target_user_id, detail::text AS detail FROM admin_audit_log"
    )
    assert (row["action"], row["target_user_id"]) == ("user.deregister", user_id)
    assert "B10000001" not in row["detail"]
