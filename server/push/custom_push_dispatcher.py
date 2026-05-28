"""Drains pending `custom_push_dispatches` rows and sends each via the
right transport. Mirrors the structure of `server/bulletins/dispatcher.py`
but stores no bulletin row — pure-notification only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import Settings
from server.models import (
    CustomPushDispatch,
    CustomPushStatus,
    DevicePlatform,
    DeviceRegistration,
)
from server.push.apns_client import SendResult
from server.push.payload import (
    build_custom_push_popup_apns,
    build_custom_push_popup_fcm,
)
from server.push.router import PushRouter

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3

# Serializes the periodic scheduler tick against the immediate kick fired
# from `POST /custom-push`. Without it, both could SELECT the same
# `pending` rows and double-send. Module-level Lock is fine because the
# backend runs as a single process; if that ever changes, swap for a
# PostgreSQL advisory lock or `with_for_update(skip_locked=True)`.
_dispatch_lock = asyncio.Lock()


async def dispatch_pending_custom_pushes(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    *,
    batch_limit: int = 100,
) -> None:
    async with _dispatch_lock:
        await _dispatch_pending_custom_pushes_locked(
            session_factory, router, settings, batch_limit=batch_limit
        )


async def _dispatch_pending_custom_pushes_locked(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    *,
    batch_limit: int = 100,
) -> None:
    ts = datetime.now(timezone.utc)

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(CustomPushDispatch, DeviceRegistration)
                .join(
                    DeviceRegistration,
                    DeviceRegistration.device_id == CustomPushDispatch.device_id,
                )
                .where(
                    CustomPushDispatch.status == CustomPushStatus.pending.value,
                )
                .order_by(CustomPushDispatch.id)
                .limit(batch_limit)
            )
        ).all()

        if not rows:
            return

        android_requests = []
        android_dispatch_ids: list[int] = []

        for dispatch, device in rows:
            if device.platform == DevicePlatform.android.value:
                # Android stores the FCM registration token in
                # `pts_token_hex` (the column is platform-overloaded so
                # we don't need a schema change for the second channel).
                if not device.pts_token_hex:
                    _cancel(dispatch, "android device has no FCM token", ts)
                    continue
                android_requests.append(
                    build_custom_push_popup_fcm(
                        fcm_token=device.pts_token_hex,
                        title=dispatch.title,
                        body=dispatch.body,
                        notification_id=dispatch.notification_id,
                        force_ring=dispatch.force_ring,
                    )
                )
                android_dispatch_ids.append(dispatch.id)
                continue
            # Apple path. APNs alert pushes use the standard device token,
            # not the Live Activity PTS token — `pts_token_hex` would
            # silently 400 on Apple's side because that token's topic
            # ends in `.push-type.liveactivity`.
            if not device.device_token_hex:
                _cancel(dispatch, "apple device has no standard APNs token", ts)
                continue
            req = build_custom_push_popup_apns(
                device_token=device.device_token_hex,
                bundle_id=device.bundle_id,
                title=dispatch.title,
                body=dispatch.body,
                notification_id=dispatch.notification_id,
                force_ring=dispatch.force_ring,
                now=ts,
            )
            try:
                result = await router.send_apple(req)
            except Exception as exc:
                result = SendResult(
                    success=False, status="exception", description=str(exc)
                )
            _mark(dispatch, result, ts)

        if android_requests:
            try:
                android_results = await router.send_android_multi(android_requests)
            except Exception as exc:
                android_results = [
                    SendResult(success=False, status="exception", description=str(exc))
                    for _ in android_requests
                ]
            results_by_id = dict(zip(android_dispatch_ids, android_results))
            for dispatch, _ in rows:
                if dispatch.id in results_by_id:
                    _mark(dispatch, results_by_id[dispatch.id], ts)

        await session.commit()


def _cancel(dispatch: CustomPushDispatch, reason: str, ts: datetime) -> None:
    """Terminal-fail a dispatch that can't be sent (missing token, etc.)
    so it doesn't get retried forever on the next scheduler tick.
    """
    dispatch.status = CustomPushStatus.failed.value
    dispatch.attempts += 1
    dispatch.last_error = reason
    logger.info(
        "custom_push.dispatch.cancelled",
        dispatch_id=dispatch.id,
        device_id=dispatch.device_id,
        reason=reason,
    )


def _mark(dispatch: CustomPushDispatch, result: SendResult, ts: datetime) -> None:
    dispatch.attempts += 1
    if result.success:
        dispatch.status = CustomPushStatus.sent.value
        dispatch.sent_at = ts
        dispatch.last_error = None
    elif dispatch.attempts >= MAX_ATTEMPTS:
        dispatch.status = CustomPushStatus.failed.value
        dispatch.last_error = result.description
    else:
        dispatch.last_error = result.description
    logger.info(
        "custom_push.dispatch",
        dispatch_id=dispatch.id,
        device_id=dispatch.device_id,
        status=dispatch.status,
        success=result.success,
    )
