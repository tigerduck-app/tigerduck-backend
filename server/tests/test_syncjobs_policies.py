"""Default sync_policies seeding (idempotent)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.syncjobs.models import SyncPolicy
from server.syncjobs.policies import ensure_default_policies

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXPECTED = {
    "moodle_assignments": (28800, True),
    "ntust_courses": (28800, True),
    "calendar": (604800, True),
    "grades": (28800, False),
}


async def test_seeds_four_default_policies(db_session):
    await ensure_default_policies(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(SyncPolicy))).scalars().all()
    assert {
        r.job_type: (r.default_interval_seconds, r.enabled) for r in rows
    } == EXPECTED


async def test_seeding_is_idempotent_and_keeps_admin_edits(db_session):
    await ensure_default_policies(db_session)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(SyncPolicy.job_type == "ntust_courses")
        )
    ).scalar_one()
    row.enabled = False
    row.default_interval_seconds = 3600
    await db_session.commit()

    await ensure_default_policies(db_session)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(SyncPolicy.job_type == "ntust_courses")
        )
    ).scalar_one()
    assert row.enabled is False
    assert row.default_interval_seconds == 3600
