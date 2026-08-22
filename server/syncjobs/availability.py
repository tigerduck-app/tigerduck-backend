"""Moodle availability check for the sync executor.

Three layers, checked in order:
1. Manual suspend (portal operator) — system_settings.moodle_suspended_until
2. Default maintenance window (.env MOODLE_MAINTENANCE_START/END)
3. Otherwise: Moodle is considered available
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import Settings
from server.system_settings import SystemSetting

logger = structlog.get_logger(__name__)

SUSPENDED_UNTIL_KEY = "moodle_suspended_until"

try:
    import zoneinfo
    _TAIPEI = zoneinfo.ZoneInfo("Asia/Taipei")
except ImportError:
    import pytz  # type: ignore[import-untyped]
    _TAIPEI = pytz.timezone("Asia/Taipei")


async def is_moodle_available(
    session: AsyncSession,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """Returns (available, resume_at).

    If Moodle is considered unavailable (maintenance or manual suspend),
    returns (False, <when to retry>). Otherwise (True, None).
    """
    if now is None:
        now = datetime.now(UTC)

    resume = await _check_manual_suspend(session, now)
    if resume is not None:
        return False, resume

    resume = _check_maintenance_window(settings, now)
    if resume is not None:
        return False, resume

    return True, None


async def _check_manual_suspend(
    session: AsyncSession, now: datetime
) -> datetime | None:
    row = await session.get(SystemSetting, SUSPENDED_UNTIL_KEY)
    if row is None:
        return None
    until_iso = row.value.get("until")
    if not isinstance(until_iso, str):
        return None
    try:
        until = datetime.fromisoformat(until_iso)
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if now < until:
        return until
    return None


def _check_maintenance_window(
    settings: Settings, now: datetime
) -> datetime | None:
    try:
        start = time.fromisoformat(settings.moodle_maintenance_start)
        end = time.fromisoformat(settings.moodle_maintenance_end)
    except ValueError:
        return None

    local_now = now.astimezone(_TAIPEI)
    local_time = local_now.time()

    if start <= end:
        in_window = start <= local_time < end
    else:
        in_window = local_time >= start or local_time < end

    if not in_window:
        return None

    end_dt = local_now.replace(
        hour=end.hour, minute=end.minute, second=0, microsecond=0
    )
    if end_dt <= local_now:
        end_dt += timedelta(days=1)
    return end_dt.astimezone(UTC)
