"""The portal's Semester dates page (`portal/app/routes/academic_calendar.py`).
In the server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.academic_calendar import get_calendar  # noqa: E402
from server.academic_calendar.models import (  # noqa: E402
    AcademicHoliday,
    UserHolidayOverride,
)
from server.auth.models import User  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session):
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


async def test_a_holiday_counts_the_users_who_opted_in(db_session, pool) -> None:
    national = AcademicHoliday(
        name_zh="國慶日", name_en="National Day",
        start_date=date(2026, 10, 9), end_date=date(2026, 10, 10),
    )
    anniversary = AcademicHoliday(
        name_zh="校慶", name_en="Anniversary",
        start_date=date(2026, 11, 12), end_date=date(2026, 11, 12),
    )
    users = [User(student_id=f"b{n}") for n in range(3)]
    db_session.add_all([national, anniversary, *users])
    await db_session.flush()
    # The third user turned the override back off, which keeps the row.
    db_session.add_all(
        UserHolidayOverride(user_id=u.id, holiday_id=national.id, notify=notify)
        for u, notify in zip(users, (True, True, False), strict=True)
    )
    await db_session.commit()

    body = json.loads((await get_calendar(pool=pool)).body)

    assert {h["name_en"]: h["overrides"] for h in body["holidays"]} == {
        "National Day": 2,
        "Anniversary": 0,
    }
