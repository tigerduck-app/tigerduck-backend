"""Device registration endpoints."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from server.db import SessionDep
from server.models import DeviceRegistration
from server.schemas import (
    DevicePreferencesRequest,
    DevicePreferencesResponse,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    DeviceUnregisterRequest,
)
from server.security import require_shared_secret

router = APIRouter(
    prefix="/devices",
    tags=["devices"],
    dependencies=[Depends(require_shared_secret)],
)
logger = structlog.get_logger(__name__)


class DeviceListItem(BaseModel):
    """Operator-facing summary of a `device_registrations` row.

    Token columns are reported as booleans only — the raw hex never
    leaves the backend container. The portal's devices page reads this
    via the `/api/devices` proxy.
    """

    device_id: str
    user_id: str
    platform: str
    device_class: str
    bundle_id: str
    apns_env: str
    server_push_enabled: bool
    has_pts_token: bool
    has_device_token: bool
    created_at: datetime
    updated_at: datetime


class DeviceListResponse(BaseModel):
    items: list[DeviceListItem]
    total: int


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    session: SessionDep,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> DeviceListResponse:
    """List registered devices, newest first. Used by the portal admin page."""
    total = (
        await session.execute(select(func.count()).select_from(DeviceRegistration))
    ).scalar_one()
    rows = (
        await session.execute(
            select(DeviceRegistration)
            .order_by(
                DeviceRegistration.updated_at.desc(),
                DeviceRegistration.device_id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    items = [
        DeviceListItem(
            device_id=d.device_id,
            user_id=d.user_id,
            platform=d.platform,
            device_class=d.device_class,
            bundle_id=d.bundle_id,
            apns_env=d.apns_env,
            server_push_enabled=d.server_push_enabled,
            has_pts_token=bool(d.pts_token_hex),
            has_device_token=bool(d.device_token_hex),
            created_at=d.created_at,
            updated_at=d.updated_at,
        )
        for d in rows
    ]
    return DeviceListResponse(items=items, total=int(total))


@router.post("/register", response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest,
    session: SessionDep,
) -> DeviceRegisterResponse:
    """Upsert a device registration. Idempotent — called on every app launch."""
    # attrs_type / apns_env are non-null in the DB but meaningless on
    # android. Persist empty strings so the column constraint stays
    # satisfied without a migration.
    attrs_type = payload.attrs_type or ""
    apns_env = payload.apns_env or ""
    stmt = (
        pg_insert(DeviceRegistration)
        .values(
            device_id=payload.device_id,
            user_id=payload.user_id,
            platform=payload.platform,
            pts_token_hex=payload.pts_token_hex,
            device_token_hex=payload.device_token_hex,
            bundle_id=payload.bundle_id,
            attrs_type=attrs_type,
            apns_env=apns_env,
            device_class=payload.device_class,
            server_push_enabled=payload.server_push_enabled,
        )
        .on_conflict_do_update(
            index_elements=[DeviceRegistration.device_id],
            set_={
                "user_id": payload.user_id,
                "platform": payload.platform,
                "pts_token_hex": payload.pts_token_hex,
                "device_token_hex": payload.device_token_hex,
                "bundle_id": payload.bundle_id,
                "attrs_type": attrs_type,
                "apns_env": apns_env,
                "device_class": payload.device_class,
                "server_push_enabled": payload.server_push_enabled,
                "updated_at": func.now(),
            },
        )
        .returning(DeviceRegistration)
    )
    result = await session.execute(stmt)
    device = result.scalar_one()

    logger.info(
        "device.registered",
        device_id=device.device_id,
        user_id=device.user_id,
        platform=device.platform,
        apns_env=device.apns_env,
    )
    return DeviceRegisterResponse(
        device_id=device.device_id,
        user_id=device.user_id,
        platform=device.platform,
        registered_at=device.updated_at,
    )


@router.post("/unregister", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    payload: DeviceUnregisterRequest,
    session: SessionDep,
) -> None:
    """Delete the device and cascade-delete its pending pushes."""
    stmt = delete(DeviceRegistration).where(
        DeviceRegistration.device_id == payload.device_id
    )
    await session.execute(stmt)
    logger.info("device.unregistered", device_id=payload.device_id)


@router.get("/{device_id}", response_model=DeviceRegisterResponse)
async def get_device(device_id: str, session: SessionDep) -> DeviceRegisterResponse:
    """Read back a device registration (used by tests and /debug tooling)."""
    result = await session.execute(
        select(DeviceRegistration).where(DeviceRegistration.device_id == device_id)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return DeviceRegisterResponse(
        device_id=device.device_id,
        user_id=device.user_id,
        platform=device.platform,
        registered_at=device.updated_at,
    )


@router.patch(
    "/{device_id}/preferences",
    response_model=DevicePreferencesResponse,
)
async def update_device_preferences(
    device_id: str,
    payload: DevicePreferencesRequest,
    session: SessionDep,
) -> DevicePreferencesResponse:
    """Flip a single device's user-facing push preferences."""
    device = await session.get(DeviceRegistration, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    device.server_push_enabled = payload.server_push_enabled
    await session.flush()
    logger.info(
        "device.preferences.updated",
        device_id=device_id,
        server_push_enabled=payload.server_push_enabled,
    )
    return DevicePreferencesResponse(
        device_id=device.device_id,
        server_push_enabled=device.server_push_enabled,
    )
