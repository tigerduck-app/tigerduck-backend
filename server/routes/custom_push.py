"""Operator endpoints for one-off custom pushes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func as sqlfunc, insert, select

from server.bulletins import jobs as bulletin_jobs
from server.bulletins.models import (
    Bulletin,
    BulletinProcessingState,
)
from server.bulletins.taxonomy import CanonicalOrg, ContentTag
from server.db import SessionDep
from server.models import CustomPushDispatch, CustomPushStatus
from server.push.custom_push_dispatcher import dispatch_pending_custom_pushes
from server.push.custom_push_targeting import (
    TargetFilter,
    count_by_class,
    resolve_target_device_ids,
)
from server.security import require_shared_secret

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/custom-push",
    tags=["custom-push"],
    dependencies=[Depends(require_shared_secret)],
)


class _CustomPushTargetFilter(BaseModel):
    target_classes: list[Literal["iphone", "ipad", "android"]] = Field(min_length=1)
    user_id: str | None = Field(default=None, max_length=64)
    device_id: str | None = Field(default=None, max_length=128)
    list_id: int | None = Field(default=None, ge=1)


class CustomPushRequest(_CustomPushTargetFilter):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=2000)
    keeps_record: bool
    force_ring: bool


class PreviewResponse(BaseModel):
    matched: dict[str, int]


class SendResponse(BaseModel):
    request_id: str
    kind: Literal["record", "popup"]
    matched: int
    queued: int


class RecentItem(BaseModel):
    id: str
    kind: Literal["record", "popup"]
    title: str
    target_classes: list[str]
    total: int
    sent_at: datetime | None
    # True until the dispatcher has actually fanned the message out.
    # For "record" rows: Bulletin.notified_at still NULL.
    # For "popup" rows: at least one CustomPushDispatch still pending.
    # Drives the portal's "queueing" badge so an operator can tell a
    # fresh-but-not-yet-sent row apart from a real zero-match send.
    is_queueing: bool = False


@router.post("/preview", response_model=PreviewResponse)
async def preview(
    body: _CustomPushTargetFilter, session: SessionDep
) -> PreviewResponse:
    filt = TargetFilter(
        target_classes=list(body.target_classes),
        user_id=body.user_id,
        device_id=body.device_id,
        list_id=body.list_id,
    )
    counts = await count_by_class(session, filt)
    return PreviewResponse(matched=counts)


@router.post("", response_model=SendResponse)
async def send(
    body: CustomPushRequest,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> SendResponse:
    filt = TargetFilter(
        target_classes=list(body.target_classes),
        user_id=body.user_id,
        device_id=body.device_id,
        list_id=body.list_id,
    )
    now = datetime.now(timezone.utc)
    request_id = secrets.token_hex(8)  # 16 chars

    if body.keeps_record:
        external_id = f"custom-{request_id}"
        bulletin = Bulletin(
            source="custom_push",
            external_id=external_id,
            source_url="",
            title=body.title,
            title_clean=body.title,
            summary=body.body,
            body_clean=body.body,
            canonical_org=CanonicalOrg.server.value,
            content_tags=[ContentTag.server_notification.value],
            importance="normal",
            posted_at=now,
            processing_state=BulletinProcessingState.processed.value,
            processing_attempts=0,
            is_deleted=False,
            dispatch_filter_json={
                "target_classes": list(body.target_classes),
                "user_id": body.user_id,
                "device_id": body.device_id,
                "list_id": body.list_id,
                "force_ring": body.force_ring,
            },
        )
        session.add(bulletin)
        await session.flush()

        matched_ids = await resolve_target_device_ids(session, filt)
        logger.info(
            "custom_push.send.record",
            request_id=request_id,
            bulletin_id=bulletin.id,
            matched=len(matched_ids),
        )
        # Fire the bulletin dispatcher as soon as the response is on the
        # wire. SessionDep commits between the route returning and the
        # background task running, so the new Bulletin row is visible.
        # The dispatcher's module-level lock serializes against the
        # periodic scheduler tick — no double-send.
        background.add_task(_kick_bulletin_dispatch, request.app)
        return SendResponse(
            request_id=request_id,
            kind="record",
            matched=len(matched_ids),
            queued=len(matched_ids),
        )

    # Pure-notification path
    matched_ids = await resolve_target_device_ids(session, filt)
    if matched_ids:
        target_classes = ",".join(body.target_classes)
        rows = []
        for did in matched_ids:
            nid_input = f"{request_id}:{did}".encode()
            nid = hashlib.sha1(nid_input).hexdigest()[:32]
            rows.append({
                "request_id": request_id,
                "device_id": did,
                "title": body.title,
                "body": body.body,
                "force_ring": body.force_ring,
                "notification_id": nid,
                "target_classes": target_classes,
                "status": CustomPushStatus.pending.value,
                "attempts": 0,
            })
        await session.execute(insert(CustomPushDispatch).values(rows))
    logger.info(
        "custom_push.send.popup",
        request_id=request_id,
        matched=len(matched_ids),
    )
    # Same immediate-fire pattern as the record path above. Skipped when
    # nothing matched — no rows to drain.
    if matched_ids:
        background.add_task(_kick_custom_push_dispatch, request.app)
    return SendResponse(
        request_id=request_id,
        kind="popup",
        matched=len(matched_ids),
        queued=len(matched_ids),
    )


async def _kick_custom_push_dispatch(app: FastAPI) -> None:
    """Drain pending popup dispatches immediately after a send.
    Logs and swallows exceptions — the next scheduler tick will retry
    anything left over."""
    try:
        await dispatch_pending_custom_pushes(
            app.state.session_factory,
            app.state.router,
            app.state.settings,
        )
    except Exception as exc:
        logger.warning("custom_push.kick.failed", error=str(exc))


async def _kick_bulletin_dispatch(app: FastAPI) -> None:
    """Same as `_kick_custom_push_dispatch` but for the record (bulletin) path."""
    try:
        await bulletin_jobs.dispatch_job(
            app.state.session_factory,
            app.state.router,
            app.state.settings,
        )
    except Exception as exc:
        logger.warning("custom_push.bulletin_kick.failed", error=str(exc))


@router.get("/recent", response_model=list[RecentItem])
async def recent(
    session: SessionDep,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[RecentItem]:
    from server.bulletins.models import BulletinDispatch

    # Bulletin counts via GROUP BY subquery (was N+1 — one COUNT per row).
    dispatch_counts = (
        select(
            BulletinDispatch.bulletin_id.label("bulletin_id"),
            sqlfunc.count().label("c"),
        )
        .group_by(BulletinDispatch.bulletin_id)
        .subquery()
    )
    bulletin_rows = (
        await session.execute(
            select(Bulletin, sqlfunc.coalesce(dispatch_counts.c.c, 0))
            .outerjoin(
                dispatch_counts, dispatch_counts.c.bulletin_id == Bulletin.id
            )
            .where(Bulletin.source == "custom_push")
            .order_by(Bulletin.posted_at.desc().nulls_last(), Bulletin.id.desc())
            .limit(limit)
        )
    ).all()
    items: list[RecentItem] = []
    for b, total in bulletin_rows:
        items.append(
            RecentItem(
                id=f"b{b.id}",
                kind="record",
                title=b.title,
                target_classes=(b.dispatch_filter_json or {}).get(
                    "target_classes", []
                ),
                total=int(total),
                sent_at=b.notified_at or b.posted_at,
                is_queueing=b.notified_at is None,
            )
        )

    # Aggregate popup dispatches by request_id in SQL. Previously a row-cap
    # (limit*10) could be exhausted by one large send, hiding older sends.
    popup_stmt = (
        select(
            CustomPushDispatch.request_id,
            sqlfunc.max(CustomPushDispatch.title).label("title"),
            # Same value across all rows of a request — max() just picks it.
            sqlfunc.max(CustomPushDispatch.target_classes).label("target_classes"),
            sqlfunc.count().label("total"),
            sqlfunc.max(CustomPushDispatch.created_at).label("created_at"),
            sqlfunc.max(CustomPushDispatch.sent_at).label("sent_at"),
            sqlfunc.sum(
                case(
                    (CustomPushDispatch.status == CustomPushStatus.pending.value, 1),
                    else_=0,
                )
            ).label("pending"),
        )
        .group_by(CustomPushDispatch.request_id)
        .order_by(sqlfunc.max(CustomPushDispatch.created_at).desc())
        .limit(limit)
    )
    popup_rows = (await session.execute(popup_stmt)).all()
    for req_id, title, target_classes, total, created_at, sent_at, pending in popup_rows:
        items.append(
            RecentItem(
                id=f"r{req_id}",
                kind="popup",
                title=title,
                target_classes=target_classes.split(",") if target_classes else [],
                total=int(total),
                sent_at=sent_at or created_at,
                is_queueing=int(pending or 0) > 0,
            )
        )

    items.sort(
        key=lambda i: (i.sent_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return items[:limit]
