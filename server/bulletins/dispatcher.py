"""Bulletin push fan-out.

For each bulletin that's been LLM-processed but never notified, expand it
into (device, bulletin) pairs via the matcher, write pending
`bulletin_dispatches` rows, and hand them to the `PushSender`. Each tick
processes one bulletin at a time so a single slow bulletin doesn't starve
the others, and a crash mid-fan-out leaves the DB in a recoverable state
(some rows sent, remaining still pending).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import batched

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.bulletins.matcher import match_device_ids
from server.bulletins.models import (
    FAILED_BULLETIN_RETRY_DELAY,
    Bulletin,
    BulletinDispatch,
    BulletinDispatchStatus,
    BulletinProcessingState,
)
from server.config import Settings
from server.db import INSERT_CHUNK_ROWS, lift_timeouts
from server.models import DevicePlatform, DeviceRegistration
from server.push.apns_client import SendResult
from server.push.custom_push_targeting import (
    TargetFilter,
    resolve_target_device_ids,
)
from server.push.fcm_client import is_dead_fcm_token
from server.push.payload import (
    ApnsRequest,
    FcmRequest,
    build_alert_request,
    build_fcm_alert_request,
)
from server.push.router import PushRouter

logger = structlog.get_logger(__name__)

MAX_DISPATCH_ATTEMPTS = 3
# Google caps a `send_each` call at 500 messages.
FCM_BATCH = 500


@dataclass(frozen=True)
class DispatchOutcome:
    bulletins_handled: int
    pushes_sent: int
    pushes_failed: int
    pushes_cancelled: int
    devices_matched: int


async def dispatch_pending_bulletins(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    *,
    now: datetime | None = None,
    batch_limit: int = 5,
) -> DispatchOutcome:
    """Fan out up to `batch_limit` bulletins per tick."""
    ts = now or datetime.now(timezone.utc)
    totals = [0, 0, 0, 0, 0]  # sent, failed, cancelled, devices, bulletins

    async with session_factory() as session:
        bulletins = (
            (
                await session.execute(
                    select(Bulletin)
                    .where(
                        Bulletin.processing_state
                        == BulletinProcessingState.processed.value,
                        Bulletin.notified_at.is_(None),
                        Bulletin.is_deleted.is_(False),
                        or_(
                            Bulletin.fanout_retry_at.is_(None),
                            Bulletin.fanout_retry_at <= ts,
                        ),
                    )
                    .order_by(Bulletin.id)
                    .limit(batch_limit)
                )
            )
            .scalars()
            .all()
        )

    for bulletin in bulletins:
        try:
            sent, failed, cancelled, matched = await _dispatch_one(
                session_factory, router, settings, bulletin.id, ts
            )
        except Exception as exc:  # noqa: BLE001 — one bulletin's guard
            # Left with notified_at unset for the next tick to retry. Raised,
            # it would stop every later bulletin: each tick starts with the
            # oldest.
            logger.exception(
                "bulletins.dispatch.bulletin_failed",
                bulletin_id=bulletin.id,
                error=str(exc)[:200],
            )
            await _delay_retry(session_factory, bulletin.id, ts)
            continue
        totals[0] += sent
        totals[1] += failed
        totals[2] += cancelled
        totals[3] += matched
        totals[4] += 1

    outcome = DispatchOutcome(
        bulletins_handled=totals[4],
        pushes_sent=totals[0],
        pushes_failed=totals[1],
        pushes_cancelled=totals[2],
        devices_matched=totals[3],
    )
    if totals[4]:
        logger.info("bulletins.dispatch.tick", **outcome.__dict__)
    return outcome


async def _delay_retry(
    session_factory: async_sessionmaker[AsyncSession], bulletin_id: int, ts: datetime
) -> None:
    """Skip a bulletin whose fan-out failed for a while, or one that keeps
    failing holds a slot of every batch and the bulletins after it wait."""
    try:
        async with session_factory() as session:
            await session.execute(
                update(Bulletin)
                .where(Bulletin.id == bulletin_id)
                .values(fanout_retry_at=ts + FAILED_BULLETIN_RETRY_DELAY)
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — the next tick tries it again
        logger.warning(
            "bulletins.dispatch.retry_not_delayed",
            bulletin_id=bulletin_id,
            error=str(exc)[:200],
        )


async def _dispatch_one(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    bulletin_id: int,
    ts: datetime,
) -> tuple[int, int, int, int]:
    """Fan out a single bulletin. Returns (sent, failed, cancelled, matched)."""
    # Step 1: resolve matching devices + insert pending dispatches in one tx.
    async with session_factory() as session:
        bulletin = await session.get(Bulletin, bulletin_id)
        if bulletin is None or bulletin.canonical_org is None:
            return (0, 0, 0, 0)

        if bulletin.dispatch_filter_json:
            # Custom-push bulletins (`source='custom_push'`) skip the
            # per-device subscription matcher entirely — the operator
            # already picked the audience via TargetFilter at send time
            # and we stamped it onto the row.
            filt = TargetFilter(
                target_classes=bulletin.dispatch_filter_json.get(
                    "target_classes", []
                ),
                user_id=bulletin.dispatch_filter_json.get("user_id"),
                device_id=bulletin.dispatch_filter_json.get("device_id"),
                list_id=bulletin.dispatch_filter_json.get("list_id"),
            )
            device_ids = await resolve_target_device_ids(session, filt)
        else:
            device_ids = await match_device_ids(
                session,
                canonical_org=bulletin.canonical_org,
                content_tags=bulletin.content_tags or [],
            )

        for chunk in batched(device_ids, INSERT_CHUNK_ROWS):
            await session.execute(
                pg_insert(BulletinDispatch)
                .values(
                    [
                        {
                            "bulletin_id": bulletin_id,
                            "device_id": did,
                            "status": BulletinDispatchStatus.pending.value,
                        }
                        for did in chunk
                    ]
                )
                # Re-runs after a crash are idempotent thanks to the
                # (bulletin_id, device_id) unique key.
                .on_conflict_do_nothing(
                    index_elements=["bulletin_id", "device_id"]
                )
            )
        await session.commit()

    # Step 2: send each pending dispatch and commit its outcome before the
    # next send, so a restart mid-fan-out resends to no device already
    # reached (#91).
    counts = {"sent": 0, "failed": 0, "cancelled": 0, "retry": 0}
    async with session_factory() as session:
        await lift_timeouts(session)
        pending_rows = (
            await session.execute(
                select(BulletinDispatch, DeviceRegistration, Bulletin)
                .join(
                    DeviceRegistration,
                    DeviceRegistration.device_id == BulletinDispatch.device_id,
                )
                .join(Bulletin, Bulletin.id == BulletinDispatch.bulletin_id)
                .where(
                    BulletinDispatch.bulletin_id == bulletin_id,
                    BulletinDispatch.status
                    == BulletinDispatchStatus.pending.value,
                )
            )
        ).all()

        # Android sends share title/body/data within a bulletin and differ
        # only by token, so they go out FCM_BATCH at a time, each batch
        # recorded before the next. Apple stays one request per row.
        android_rows = [
            row
            for row in pending_rows
            if row[1].platform == DevicePlatform.android.value and row[1].pts_token_hex
        ]
        for chunk in batched(android_rows, FCM_BATCH):
            requests = []
            for _dispatch, device, bulletin in chunk:
                kind, force_ring = _kind_and_force_ring(bulletin)
                requests.append(
                    build_fcm_alert_request(
                        fcm_token=device.pts_token_hex,
                        title=bulletin.title_clean or bulletin.title,
                        body=bulletin.summary or bulletin.title_clean or bulletin.title,
                        bulletin_id=bulletin.id,
                        source_url=bulletin.source_url,
                        canonical_org=bulletin.canonical_org or "",
                        kind=kind,
                        force_ring=force_ring,
                    )
                )
            try:
                results = await router.send_android_multi(requests)
            except Exception as exc:  # firebase-admin can still raise on init/auth
                results = [
                    SendResult(success=False, status="exception", description=str(exc))
                    for _ in requests
                ]
            for (dispatch, device, _bulletin), result in zip(chunk, results):
                counts[await _record(session, dispatch, device, result, ts)] += 1
            await session.commit()

        for dispatch, device, bulletin in pending_rows:
            if device.platform == DevicePlatform.android.value:
                # Android stores the FCM registration token in
                # `pts_token_hex` (the column is platform-overloaded so we
                # don't need a schema change for the second channel).
                if device.pts_token_hex:
                    continue  # sent above
                await _cancel_dispatch(
                    session, dispatch.id, "android device has no FCM token"
                )
                counts["cancelled"] += 1
                await session.commit()
                continue
            if not device.device_token_hex:
                await _cancel_dispatch(
                    session, dispatch.id, "device has no standard APNs token"
                )
                counts["cancelled"] += 1
                await session.commit()
                continue
            # Bulletins arrive here in `processed` state, so `title_clean`
            # has already been normalized by the LLM (prefix stripped,
            # de-shouted). Fall back to the raw title when classification
            # left it NULL — mirrors iOS `BulletinAPIDTO.displayTitle`.
            kind, force_ring = _kind_and_force_ring(bulletin)
            apns_req = build_alert_request(
                device_token=device.device_token_hex,
                bundle_id=device.bundle_id,
                title=bulletin.title_clean or bulletin.title,
                body=bulletin.summary or bulletin.title_clean or bulletin.title,
                bulletin_id=bulletin.id,
                source_url=bulletin.source_url,
                canonical_org=bulletin.canonical_org or "",
                now=ts,
                kind=kind,
                force_ring=force_ring,
            )
            result = await _send_apple_safely(router, apns_req)
            counts[await _record(session, dispatch, device, result, ts)] += 1
            await session.commit()

        # Step 3: if every dispatch row for this bulletin is terminal
        # (sent/failed/cancelled), stamp notified_at so the next tick
        # doesn't pick the bulletin up again.
        still_pending = (
            await session.execute(
                select(BulletinDispatch.id)
                .where(
                    BulletinDispatch.bulletin_id == bulletin_id,
                    BulletinDispatch.status
                    == BulletinDispatchStatus.pending.value,
                )
                .limit(1)
            )
        ).first()
        if still_pending is None:
            await session.execute(
                update(Bulletin)
                .where(Bulletin.id == bulletin_id)
                .values(notified_at=ts)
            )

        await session.commit()

    return (counts["sent"], counts["failed"], counts["cancelled"], len(pending_rows))


async def _record(
    session: AsyncSession,
    dispatch: BulletinDispatch,
    device: DeviceRegistration,
    result: SendResult,
    ts: datetime,
) -> str:
    """Write one send's outcome to its dispatch row: `sent`, `cancelled` for a
    dead token, `failed` once out of attempts, else `retry`."""
    classification = _classify(result, platform=device.platform)
    attempts = dispatch.attempts + 1
    # Each update skips the session sync: it would check every dispatch row
    # this session holds, quadratic in the bulletin's devices (#63).
    row = (
        update(BulletinDispatch)
        .execution_options(synchronize_session=False)
        .where(BulletinDispatch.id == dispatch.id)
    )
    if classification == "sent":
        await session.execute(
            row.values(
                status=BulletinDispatchStatus.sent.value,
                attempts=attempts,
                sent_at=ts,
                last_error=None,
            )
        )
        return "sent"
    if classification == "bad_token":
        await session.execute(
            row.values(
                status=BulletinDispatchStatus.cancelled.value,
                attempts=attempts,
                last_error=result.description or "bad_token",
            )
        )
        await _forget_dead_token(session, device)
        return "cancelled"
    error = f"status={result.status} desc={result.description}"
    if attempts >= MAX_DISPATCH_ATTEMPTS:
        await session.execute(
            row.values(
                status=BulletinDispatchStatus.failed.value,
                attempts=attempts,
                last_error=error,
            )
        )
        return "failed"
    await session.execute(row.values(attempts=attempts, last_error=error))
    return "retry"


async def _forget_dead_token(session: AsyncSession, device: DeviceRegistration) -> None:
    """Drop the token the provider refused, so no later bulletin targets it.
    Left alone when the app registered another meanwhile; its next launch
    registers a working one (`POST /v3/devices/anonymous`)."""
    if device.platform == DevicePlatform.android.value:
        column, cleared = DeviceRegistration.pts_token_hex, ""
        refused = device.pts_token_hex
    else:
        column, cleared = DeviceRegistration.device_token_hex, None
        refused = device.device_token_hex
    await session.execute(
        update(DeviceRegistration)
        .execution_options(synchronize_session=False)
        .where(DeviceRegistration.device_id == device.device_id, column == refused)
        .values({column: cleared})
    )


def _kind_and_force_ring(bulletin: Bulletin) -> tuple[str, bool]:
    """Custom-push bulletins ride the same dispatcher but carry
    `dispatch_filter_json` with operator-chosen `force_ring`; legacy
    scraped bulletins fall back to the historic defaults so existing
    payloads remain byte-identical."""
    if bulletin.dispatch_filter_json:
        return (
            "custom_push_bulletin",
            bool(bulletin.dispatch_filter_json.get("force_ring", True)),
        )
    return ("bulletin", True)


async def _cancel_dispatch(
    session: AsyncSession, dispatch_id: int, reason: str
) -> None:
    await session.execute(
        update(BulletinDispatch)
        .execution_options(synchronize_session=False)
        .where(BulletinDispatch.id == dispatch_id)
        .values(
            status=BulletinDispatchStatus.cancelled.value,
            last_error=reason,
        )
    )


async def _send_apple_safely(router: PushRouter, request: ApnsRequest) -> SendResult:
    try:
        return await router.send_apple(request)
    except Exception as exc:  # aioapns raises on network errors
        return SendResult(success=False, status="exception", description=str(exc))


def _classify(result: SendResult, *, platform: str) -> str:
    """Map an APNs/FCM result onto 'sent' | 'bad_token' | 'transient'.

    Was a deliberate copy of the one in `scheduler/dispatcher`, so a change
    to one push path couldn't mutate the other. That module went with the
    v2 sunset; this is now the bulletin path's own.
    """
    if result.success:
        return "sent"
    status = str(result.status).lower()
    desc = (result.description or "").lower()

    if platform == DevicePlatform.android.value:
        if is_dead_fcm_token(result.status, result.description):
            return "bad_token"
        return "transient"

    bad_token_markers = (
        "410",
        "baddevicetoken",
        "bad_device_token",
        "unregistered",
        "devicetokennotfortopic",
    )
    if any(m in status or m in desc for m in bad_token_markers):
        return "bad_token"
    return "transient"
