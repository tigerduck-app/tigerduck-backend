"""Per-user holiday exceptions — "notify me anyway on this holiday".

The holidays themselves are school-wide operator data served unauthenticated
from `/v3/calendar/semesters`; only the exception is per user, so only the
exception lives here behind a session.

A device with cloud sync off never calls these — it keeps the choice in its
own preferences and the user's other devices stay quiet. That asymmetry is
deliberate: the guard itself has to work for everyone, signed in or not,
while agreeing across devices is a cloud-sync feature like every other
override.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from server.academic_calendar.models import AcademicHoliday, UserHolidayOverride
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync.changelog import append_change
from server.sync.models import ChangeEntityType

router = APIRouter(prefix="/holiday-overrides", tags=["sync-overrides"])
logger = structlog.get_logger(__name__)


class HolidayOverrideRequest(BaseModel):
    #: True keeps class reminders on for this holiday. False is stored
    #: rather than deleted so the client can turn the exception off without
    #: the change looking like "never set it", which is what a delete would
    #: mean to a device that had not synced in between.
    notify: bool


class HolidayOverrideOut(BaseModel):
    holiday_id: int
    notify: bool


class HolidayOverrideListResponse(BaseModel):
    overrides: list[HolidayOverrideOut]


@router.get("", response_model=HolidayOverrideListResponse)
async def list_overrides(
    auth: CurrentAuthDep, session: SessionDep
) -> HolidayOverrideListResponse:
    rows = (
        (
            await session.execute(
                select(UserHolidayOverride).where(
                    UserHolidayOverride.user_id == auth.user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return HolidayOverrideListResponse(
        overrides=[
            HolidayOverrideOut(holiday_id=r.holiday_id, notify=r.notify) for r in rows
        ]
    )


@router.put("/{holiday_id}", response_model=HolidayOverrideOut)
async def set_override(
    holiday_id: int,
    payload: HolidayOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
) -> HolidayOverrideOut:
    """Set this user's exception for one holiday.

    Rejects an unknown holiday rather than storing the row: the id comes
    from the public feed, so a miss means the client is working from a
    calendar the server has since changed, and silently accepting it would
    leave a row that can never be matched to anything.
    """
    exists = (
        await session.execute(
            select(AcademicHoliday.id).where(AcademicHoliday.id == holiday_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="holiday_not_found"
        )

    existing = (
        await session.execute(
            select(UserHolidayOverride).where(
                UserHolidayOverride.user_id == auth.user_id,
                UserHolidayOverride.holiday_id == holiday_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = UserHolidayOverride(user_id=auth.user_id, holiday_id=holiday_id)
        session.add(existing)
    existing.notify = payload.notify
    existing.updated_at = datetime.now(UTC)
    await session.flush()

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.holiday_override.value,
        entity_id=str(holiday_id),
        operation="upsert",
        payload={"holiday_id": holiday_id, "notify": payload.notify},
        device_id=auth.device_id,
    )
    await session.commit()

    logger.info(
        "holiday_override.set",
        user_id=str(auth.user_id),
        holiday_id=holiday_id,
        notify=payload.notify,
    )
    return HolidayOverrideOut(holiday_id=holiday_id, notify=existing.notify)
