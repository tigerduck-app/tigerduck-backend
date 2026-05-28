"""Operator-managed device lists.

A "list" is a named bag of `device_registrations.device_id` values that
custom-push can target by id (see `TargetFilter.list_id`). The CRUD lives
here; membership add/remove is also here so the portal doesn't have to
juggle two routers for what is one UX surface.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from server.db import SessionDep
from server.models import DeviceList, DeviceListMember, DeviceRegistration
from server.security import require_shared_secret

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/device-lists",
    tags=["device-lists"],
    dependencies=[Depends(require_shared_secret)],
)


class DeviceListSummary(BaseModel):
    id: int
    name: str
    description: str | None
    member_count: int
    created_at: datetime
    updated_at: datetime


class DeviceListCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        # min_length runs on the raw value, so trim here and reject a
        # whitespace-only name rather than storing "" (an unusable list
        # that also burns the unique empty-name slot).
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class DeviceListUpdateRequest(BaseModel):
    # All-optional patch payload — omit a field to leave it unchanged.
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class DeviceListMemberItem(BaseModel):
    device_id: str
    user_id: str
    platform: str
    device_class: str
    added_at: datetime


class DeviceListMembersResponse(BaseModel):
    list_id: int
    list_name: str
    items: list[DeviceListMemberItem]
    total: int


class AddMembersRequest(BaseModel):
    device_ids: list[str] = Field(min_length=1, max_length=10000)


class AddMembersResponse(BaseModel):
    added: int
    already_present: int
    unknown: int


@router.get("", response_model=list[DeviceListSummary])
async def list_lists(session: SessionDep) -> list[DeviceListSummary]:
    """All lists, newest first, with member counts."""
    member_count = (
        select(
            DeviceListMember.list_id.label("list_id"),
            func.count().label("c"),
        )
        .group_by(DeviceListMember.list_id)
        .subquery()
    )
    stmt = (
        select(DeviceList, func.coalesce(member_count.c.c, 0))
        .outerjoin(member_count, member_count.c.list_id == DeviceList.id)
        .order_by(DeviceList.updated_at.desc(), DeviceList.id.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        DeviceListSummary(
            id=lst.id,
            name=lst.name,
            description=lst.description,
            member_count=int(cnt),
            created_at=lst.created_at,
            updated_at=lst.updated_at,
        )
        for lst, cnt in rows
    ]


@router.post("", response_model=DeviceListSummary, status_code=status.HTTP_201_CREATED)
async def create_list(
    body: DeviceListCreateRequest, session: SessionDep
) -> DeviceListSummary:
    lst = DeviceList(name=body.name, description=body.description)
    session.add(lst)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Unique constraint on name — surface as 409 so the portal can
        # show a friendly "name already exists" instead of a generic 500.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"list name {body.name!r} already exists",
        ) from exc
    logger.info("device_list.created", list_id=lst.id, name=lst.name)
    return DeviceListSummary(
        id=lst.id,
        name=lst.name,
        description=lst.description,
        member_count=0,
        created_at=lst.created_at,
        updated_at=lst.updated_at,
    )


@router.get("/{list_id}", response_model=DeviceListSummary)
async def get_list(list_id: int, session: SessionDep) -> DeviceListSummary:
    lst = await session.get(DeviceList, list_id)
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")
    count = (
        await session.execute(
            select(func.count())
            .select_from(DeviceListMember)
            .where(DeviceListMember.list_id == list_id)
        )
    ).scalar_one()
    return DeviceListSummary(
        id=lst.id,
        name=lst.name,
        description=lst.description,
        member_count=int(count),
        created_at=lst.created_at,
        updated_at=lst.updated_at,
    )


@router.patch("/{list_id}", response_model=DeviceListSummary)
async def update_list(
    list_id: int,
    body: DeviceListUpdateRequest,
    session: SessionDep,
) -> DeviceListSummary:
    lst = await session.get(DeviceList, list_id)
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")
    # Use model_fields_set so PATCH can distinguish "omitted" (leave
    # unchanged) from "explicitly null" (clear to NULL). Without this the
    # caller has no way to clear `description` once set.
    provided = body.model_fields_set
    if "name" in provided and body.name is not None:
        lst.name = body.name
    if "description" in provided:
        lst.description = body.description
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"list name {body.name!r} already exists",
        ) from exc
    count = (
        await session.execute(
            select(func.count())
            .select_from(DeviceListMember)
            .where(DeviceListMember.list_id == list_id)
        )
    ).scalar_one()
    return DeviceListSummary(
        id=lst.id,
        name=lst.name,
        description=lst.description,
        member_count=int(count),
        created_at=lst.created_at,
        updated_at=lst.updated_at,
    )


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_list(list_id: int, session: SessionDep) -> None:
    res = await session.execute(delete(DeviceList).where(DeviceList.id == list_id))
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="list not found")
    logger.info("device_list.deleted", list_id=list_id)


@router.get("/{list_id}/members", response_model=DeviceListMembersResponse)
async def list_members(
    list_id: int,
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> DeviceListMembersResponse:
    lst = await session.get(DeviceList, list_id)
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")
    total = (
        await session.execute(
            select(func.count())
            .select_from(DeviceListMember)
            .where(DeviceListMember.list_id == list_id)
        )
    ).scalar_one()
    stmt = (
        select(
            DeviceListMember.device_id,
            DeviceListMember.added_at,
            DeviceRegistration.user_id,
            DeviceRegistration.platform,
            DeviceRegistration.device_class,
        )
        .join(
            DeviceRegistration,
            DeviceRegistration.device_id == DeviceListMember.device_id,
        )
        .where(DeviceListMember.list_id == list_id)
        .order_by(DeviceListMember.added_at.desc(), DeviceListMember.device_id)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    items = [
        DeviceListMemberItem(
            device_id=did,
            user_id=uid,
            platform=plat,
            device_class=dclass,
            added_at=added,
        )
        for did, added, uid, plat, dclass in rows
    ]
    return DeviceListMembersResponse(
        list_id=list_id, list_name=lst.name, items=items, total=int(total)
    )


@router.post("/{list_id}/members", response_model=AddMembersResponse)
async def add_members(
    list_id: int,
    body: AddMembersRequest,
    session: SessionDep,
) -> AddMembersResponse:
    lst = await session.get(DeviceList, list_id)
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")

    requested = list(dict.fromkeys(body.device_ids))  # de-dupe, preserve order
    # Filter to device_ids that actually exist — anything else gets
    # bucketed as "unknown" rather than 500ing via FK violation.
    existing = set(
        (
            await session.execute(
                select(DeviceRegistration.device_id).where(
                    DeviceRegistration.device_id.in_(requested)
                )
            )
        ).scalars().all()
    )
    valid = [d for d in requested if d in existing]
    unknown = len(requested) - len(valid)

    if not valid:
        return AddMembersResponse(added=0, already_present=0, unknown=unknown)

    # ON CONFLICT DO NOTHING so a partial overlap with the existing list
    # is idempotent — the operator can re-submit a CSV without errors.
    stmt = (
        pg_insert(DeviceListMember)
        .values([{"list_id": list_id, "device_id": d} for d in valid])
        .on_conflict_do_nothing(
            index_elements=[DeviceListMember.list_id, DeviceListMember.device_id]
        )
    )
    result = await session.execute(stmt)
    added = int(result.rowcount or 0)
    already_present = len(valid) - added

    # Bump updated_at on the parent list so the lists table sort surfaces
    # recent activity even though no DeviceList columns changed.
    lst.updated_at = func.now()  # type: ignore[assignment]

    logger.info(
        "device_list.members.added",
        list_id=list_id,
        requested=len(requested),
        added=added,
        already_present=already_present,
        unknown=unknown,
    )
    return AddMembersResponse(
        added=added, already_present=already_present, unknown=unknown
    )


@router.delete(
    "/{list_id}/members/{device_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    list_id: int, device_id: str, session: SessionDep
) -> None:
    res = await session.execute(
        delete(DeviceListMember).where(
            DeviceListMember.list_id == list_id,
            DeviceListMember.device_id == device_id,
        )
    )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="member not found in list")
    lst = await session.get(DeviceList, list_id)
    if lst is not None:
        lst.updated_at = func.now()  # type: ignore[assignment]
    logger.info(
        "device_list.members.removed", list_id=list_id, device_id=device_id
    )
