"""Drains pending `custom_push_dispatches` rows and sends each via the
right transport. Mirrors the structure of `server/bulletins/dispatcher.py`
but stores no bulletin row — pure-notification only.
"""

from __future__ import annotations

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


async def dispatch_pending_custom_pushes(
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
            # Apple path
            req = build_custom_push_popup_apns(
                device_token=device.pts_token_hex,
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
