"""Debug endpoints — gated behind TIGERDUCK_ENV != production.

Lets us verify the dispatcher path end-to-end without waiting for a real
fire_at to elapse. Production builds should set TIGERDUCK_ENV=production
to hide these.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.db import SessionDep
from server.models import (
    DevicePlatform,
    DeviceRegistration,
    LiveActivityTokenStatus,
    LiveActivityUpdateToken,
    PushStatus,
    ScheduledPush,
)
from server.push.payload import (
    build_alert_request,
    build_apns_request,
    build_live_activity_end_request,
    composed_activity_id,
)
from server.scheduler.dispatcher import dispatch_due_pushes
from server.security import require_shared_secret

router = APIRouter(
    prefix="/_debug",
    tags=["debug"],
    dependencies=[Depends(require_shared_secret)],
)
logger = structlog.get_logger(__name__)


def _require_dev(request: Request) -> None:
    if request.app.state.settings.env == "production":
        raise HTTPException(status_code=404, detail="not found")


@router.get("/stats")
async def stats(request: Request, session: SessionDep) -> dict:
    _require_dev(request)
    from sqlalchemy import func

    rows = await session.execute(
        select(ScheduledPush.status, func.count()).group_by(ScheduledPush.status)
    )
    by_status = dict(rows.all())
    activity_rows = await session.execute(
        select(LiveActivityUpdateToken.status, func.count()).group_by(
            LiveActivityUpdateToken.status
        )
    )
    activities_by_status = dict(activity_rows.all())
    device_count = (
        await session.execute(select(func.count()).select_from(DeviceRegistration))
    ).scalar_one()
    return {
        "devices": device_count,
        "pushes_by_status": by_status,
        "live_activities_by_status": activities_by_status,
        "sender_type": type(request.app.state.sender).__name__,
    }


@router.post("/tick")
async def force_tick(request: Request) -> dict:
    """Run one dispatcher tick immediately instead of waiting for the timer."""
    _require_dev(request)
    outcome = await dispatch_due_pushes(
        session_factory=request.app.state.session_factory,
        sender=request.app.state.sender,
        settings=request.app.state.settings,
    )
    return {
        "dispatched": outcome.dispatched,
        "sent": outcome.sent,
        "failed": outcome.failed,
        "cancelled": outcome.cancelled,
    }


@router.post("/fire/{push_id:path}")
async def fire_one(
    push_id: str,
    request: Request,
    session: SessionDep,
) -> dict:
    """Send one specific push_id now, bypassing fire_at. Status unchanged.
    For smoke-testing APNs delivery without waiting for scheduled time."""
    _require_dev(request)
    push = await session.get(ScheduledPush, push_id)
    if push is None:
        raise HTTPException(status_code=404, detail=f"no such push_id: {push_id}")
    device = await session.get(DeviceRegistration, push.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device missing")

    req = build_apns_request(
        device_token=device.pts_token_hex,
        bundle_id=device.bundle_id,
        scenario=push.scenario,
        source_id=push.source_id,
        fire_at=push.fire_at,
        snapshot=push.payload_json,
        attrs_type=device.attrs_type,
        now=datetime.now(timezone.utc),
    )
    sender = request.app.state.sender
    result = await sender.send(req)
    logger.info(
        "debug.fire",
        push_id=push_id,
        success=result.success,
        status=result.status,
        desc=result.description,
    )
    return {
        "push_id": push_id,
        "apns_status": result.status,
        "success": result.success,
        "description": result.description,
    }


@router.post("/fire_first_pending")
async def fire_first_pending(
    request: Request,
    session: SessionDep,
) -> dict:
    """Grab the earliest-scheduled pending push and fire it now."""
    _require_dev(request)
    row = await session.execute(
        select(ScheduledPush)
        .where(ScheduledPush.status == PushStatus.pending.value)
        .order_by(ScheduledPush.fire_at.asc())
        .limit(1)
    )
    push = row.scalar_one_or_none()
    if push is None:
        raise HTTPException(status_code=404, detail="no pending pushes")

    device = await session.get(DeviceRegistration, push.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device missing for push")

    req = build_apns_request(
        device_token=device.pts_token_hex,
        bundle_id=device.bundle_id,
        scenario=push.scenario,
        source_id=push.source_id,
        fire_at=push.fire_at,
        snapshot=push.payload_json,
        attrs_type=device.attrs_type,
        now=datetime.now(timezone.utc),
    )
    sender = request.app.state.sender
    result = await sender.send(req)
    logger.info(
        "debug.fire_first",
        push_id=push.push_id,
        scenario=push.scenario,
        success=result.success,
        status=result.status,
        desc=result.description,
    )
    return {
        "push_id": push.push_id,
        "scenario": push.scenario,
        "title": push.payload_json.get("title"),
        "apns_status": result.status,
        "success": result.success,
        "description": result.description,
    }


# ---------------------------------------------------------------------------
# Test push surface — drives the portal's /test page.
#
# All three endpoints synthesise APNs payloads on the fly and hit the
# real `app.state.sender`, so a successful send proves the same path
# would deliver a real bulletin / Live Activity. None of them write to
# the bulletins / scheduled_pushes / live_activity_update_tokens tables,
# so the test page can fire freely without polluting prod-shaped data.
# ---------------------------------------------------------------------------


class _DeviceRow(BaseModel):
    device_id: str
    user_id: str
    platform: str
    bundle_id: str
    apns_env: str
    has_pts_token: bool
    has_device_token: bool
    active_live_activities: int
    created_at: datetime
    updated_at: datetime


@router.get("/devices", response_model=list[_DeviceRow])
async def list_devices(request: Request, session: SessionDep) -> list[_DeviceRow]:
    """Every registered device with the bits the test page needs to pick one
    and know which push channels are wired up."""
    _require_dev(request)
    from sqlalchemy import func

    rows = (
        await session.execute(
            select(DeviceRegistration).order_by(DeviceRegistration.updated_at.desc())
        )
    ).scalars().all()

    active_counts = dict(
        (
            await session.execute(
                select(
                    LiveActivityUpdateToken.device_id, func.count()
                )
                .where(
                    LiveActivityUpdateToken.status
                    == LiveActivityTokenStatus.active.value
                )
                .group_by(LiveActivityUpdateToken.device_id)
            )
        ).all()
    )

    return [
        _DeviceRow(
            device_id=d.device_id,
            user_id=d.user_id,
            platform=d.platform,
            bundle_id=d.bundle_id,
            apns_env=d.apns_env,
            has_pts_token=bool(d.pts_token_hex),
            has_device_token=bool(d.device_token_hex),
            active_live_activities=int(active_counts.get(d.device_id, 0)),
            created_at=d.created_at,
            updated_at=d.updated_at,
        )
        for d in rows
    ]


class _SendAlertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    device_ids: list[str] | None = Field(
        default=None,
        description="Apple device_ids to target. None / empty = fan-out to "
        "every apple device with a non-empty device_token_hex.",
    )


class _PerDeviceResult(BaseModel):
    device_id: str
    success: bool
    status: str
    description: str | None = None
    skipped_reason: str | None = None


class _SendAlertResponse(BaseModel):
    sent: int
    failed: int
    skipped: int
    results: list[_PerDeviceResult]


@router.post("/send_alert", response_model=_SendAlertResponse)
async def send_alert(
    payload: _SendAlertRequest, request: Request, session: SessionDep
) -> _SendAlertResponse:
    """Fan-out a synthetic alert push. Same payload shape the bulletin
    dispatcher uses, just without the bulletin metadata fields."""
    _require_dev(request)

    stmt = select(DeviceRegistration).where(
        DeviceRegistration.platform == DevicePlatform.apple.value
    )
    if payload.device_ids:
        stmt = stmt.where(DeviceRegistration.device_id.in_(payload.device_ids))
    devices = (await session.execute(stmt)).scalars().all()

    sender = request.app.state.sender
    results: list[_PerDeviceResult] = []
    sent = failed = skipped = 0
    for d in devices:
        if not d.device_token_hex:
            results.append(
                _PerDeviceResult(
                    device_id=d.device_id,
                    success=False,
                    status="skipped",
                    skipped_reason="no device_token_hex (alert token not registered)",
                )
            )
            skipped += 1
            continue
        # Synthetic bulletin id (0) + placeholder source_url keeps the
        # notification content extension happy without inserting a real row.
        req = build_alert_request(
            device_token=d.device_token_hex,
            bundle_id=d.bundle_id,
            title=payload.title,
            body=payload.body,
            bulletin_id=0,
            source_url="https://api.tigerduck.app/_debug/test_alert",
            canonical_org="test",
            thread_id="debug-test",
        )
        outcome = await sender.send(req)
        results.append(
            _PerDeviceResult(
                device_id=d.device_id,
                success=outcome.success,
                status=outcome.status,
                description=outcome.description,
            )
        )
        if outcome.success:
            sent += 1
        else:
            failed += 1
    logger.info(
        "debug.send_alert",
        targeted=len(devices),
        sent=sent,
        failed=failed,
        skipped=skipped,
    )
    return _SendAlertResponse(
        sent=sent, failed=failed, skipped=skipped, results=results
    )


# Mirror the SCENARIO_* string constants from push.payload so the portal
# can show a closed set in its picker without re-importing them.
_LIVE_ACTIVITY_SCENARIOS = ("classPreparing", "inClass", "assignmentUrgent")


class _SendLiveActivityRequest(BaseModel):
    device_id: str
    scenario: str = Field(
        description=f"One of: {', '.join(_LIVE_ACTIVITY_SCENARIOS)}"
    )
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    location_text: str = Field(default="", max_length=120)
    countdown_target_iso: str | None = Field(
        default=None,
        description="ISO8601. Drives the lock-screen countdown + APNs "
        "expiration. None = no countdown (the activity has no end time).",
    )
    source_id: str = Field(
        default="debug-test",
        description="Logical id of the thing the activity is about. Pair "
        "this with the same value across start/end to test the dedup path.",
    )


class _SendLiveActivityResponse(BaseModel):
    device_id: str
    activity_id: str
    success: bool
    status: str
    description: str | None = None
    sent_payload: dict


@router.post("/send_live_activity", response_model=_SendLiveActivityResponse)
async def send_live_activity(
    payload: _SendLiveActivityRequest, request: Request, session: SessionDep
) -> _SendLiveActivityResponse:
    """Push-to-Start a synthetic Live Activity on one device. Builds the
    same payload the schedule dispatcher would build for a real class /
    assignment, so a green result here means the scheduler path works."""
    _require_dev(request)
    if payload.scenario not in _LIVE_ACTIVITY_SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"scenario must be one of {_LIVE_ACTIVITY_SCENARIOS}",
        )
    device = await session.get(DeviceRegistration, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device_id")
    if not device.pts_token_hex:
        raise HTTPException(
            status_code=409,
            detail="device has no pts_token_hex — PTS not registered yet",
        )

    now = datetime.now(timezone.utc)
    snapshot: dict = {
        "title": payload.title,
        "subtitle": payload.subtitle,
        "locationText": payload.location_text,
    }
    if payload.countdown_target_iso:
        snapshot["countdownTarget"] = payload.countdown_target_iso
        try:
            fire_at = datetime.fromisoformat(
                payload.countdown_target_iso.replace("Z", "+00:00")
            )
        except ValueError as e:
            raise HTTPException(
                status_code=422, detail=f"bad countdown_target_iso: {e}"
            ) from e
    else:
        # No countdown → build_apns_request falls back to fire_at +
        # expiration_slack_seconds for the apns-expiration header, so
        # pointing fire_at at "now" gives roughly a 60s delivery window
        # which is fine for an interactive test push.
        fire_at = now

    req = build_apns_request(
        device_token=device.pts_token_hex,
        bundle_id=device.bundle_id,
        scenario=payload.scenario,
        source_id=payload.source_id,
        fire_at=fire_at,
        snapshot=snapshot,
        attrs_type=device.attrs_type,
        now=now,
    )
    outcome = await request.app.state.sender.send(req)
    activity_id = composed_activity_id(payload.scenario, payload.source_id)
    logger.info(
        "debug.send_live_activity",
        device_id=device.device_id,
        scenario=payload.scenario,
        activity_id=activity_id,
        success=outcome.success,
        status=outcome.status,
    )
    return _SendLiveActivityResponse(
        device_id=device.device_id,
        activity_id=activity_id,
        success=outcome.success,
        status=outcome.status,
        description=outcome.description,
        sent_payload=req.message,
    )


class _EndLiveActivityRequest(BaseModel):
    activity_id: str = Field(
        description="`<scenario>::<source_id>`, the same id "
        "send_live_activity returned."
    )


@router.post("/end_live_activity", response_model=_SendLiveActivityResponse)
async def end_live_activity(
    payload: _EndLiveActivityRequest, request: Request, session: SessionDep
) -> _SendLiveActivityResponse:
    """End a Live Activity that the device previously reported an update
    token for. Looks up the active LiveActivityUpdateToken row, builds an
    end-event payload, and fires it."""
    _require_dev(request)
    token = await session.get(LiveActivityUpdateToken, payload.activity_id)
    if token is None:
        raise HTTPException(
            status_code=404, detail="no such activity_id (no update token registered)"
        )
    if token.status != LiveActivityTokenStatus.active.value:
        raise HTTPException(
            status_code=409,
            detail=f"activity not active (status={token.status})",
        )
    device = await session.get(DeviceRegistration, token.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device for activity missing")

    req = build_live_activity_end_request(
        update_token=token.update_token_hex,
        bundle_id=device.bundle_id,
        snapshot=token.snapshot_json,
    )
    outcome = await request.app.state.sender.send(req)
    logger.info(
        "debug.end_live_activity",
        activity_id=token.activity_id,
        device_id=device.device_id,
        success=outcome.success,
        status=outcome.status,
    )
    return _SendLiveActivityResponse(
        device_id=device.device_id,
        activity_id=token.activity_id,
        success=outcome.success,
        status=outcome.status,
        description=outcome.description,
        sent_payload=req.message,
    )
