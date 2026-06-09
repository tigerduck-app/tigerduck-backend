"""Default sync_policies seed values (migration plan Phase 3).

Seeded both by the Alembic migration (production) and at app startup via
`ensure_default_policies` (covers create_all-based test DBs and fresh
environments). ON CONFLICT DO NOTHING — admin edits always win.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.syncjobs.models import SyncJobType, SyncPolicy

DEFAULT_POLICIES: tuple[dict, ...] = (
    {
        "job_type": SyncJobType.moodle_assignments.value,
        "default_interval_seconds": 28800,
        "enabled": True,
    },
    {
        "job_type": SyncJobType.ntust_courses.value,
        "default_interval_seconds": 28800,
        "enabled": False,
    },
    {
        "job_type": SyncJobType.calendar.value,
        "default_interval_seconds": 604800,
        "enabled": True,
    },
    {
        "job_type": SyncJobType.grades.value,
        "default_interval_seconds": 28800,
        "enabled": False,
    },
)


async def ensure_default_policies(session: AsyncSession) -> None:
    await session.execute(
        pg_insert(SyncPolicy)
        .values(list(DEFAULT_POLICIES))
        .on_conflict_do_nothing(index_elements=["job_type"])
    )
