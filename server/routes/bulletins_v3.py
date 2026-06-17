"""/v3 user-level bulletin subscriptions (per-rule CRUD with revision
optimistic concurrency) and read/starred/hidden states (per-field merge).

Anonymous devices keep using /v2/devices/{id}/subscriptions; this router is
for logged-in users only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.auth.dependencies import CurrentAuthDep
from server.bulletins.models import Bulletin
from server.db import SessionDep
from server.sync import serializers
from server.sync.changelog import append_change
from server.sync.merge import clamp_ts
from server.sync.models import UserBulletinState, UserBulletinSubscription

subscriptions_router = APIRouter(
    prefix="/bulletin-subscriptions", tags=["bulletin-subscriptions"]
)
states_router = APIRouter(prefix="/bulletin-states", tags=["bulletin-states"])
logger = structlog.get_logger(__name__)


class SubscriptionCreate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    orgs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mode: Literal["AND", "OR"] = "AND"
    enabled: bool = True


class SubscriptionPatch(BaseModel):
    base_revision: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=128)
    orgs: list[str] | None = None
    tags: list[str] | None = None
    mode: Literal["AND", "OR"] | None = None
    enabled: bool | None = None


class BulletinStatePut(BaseModel):
    """Per-field triplets; `<field>_updated_at` present == edited."""

    is_read: bool | None = None
    read_updated_at: datetime | None = None
    is_starred: bool | None = None
    starred_updated_at: datetime | None = None
    is_hidden: bool | None = None
    hidden_updated_at: datetime | None = None


async def _get_owned_subscription(
    session, user_id, subscription_id: int
) -> UserBulletinSubscription:
    row = (
        await session.execute(
            select(UserBulletinSubscription).where(
                UserBulletinSubscription.id == subscription_id,
                UserBulletinSubscription.user_id == user_id,
                UserBulletinSubscription.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return row


@subscriptions_router.get("")
async def list_subscriptions(auth: CurrentAuthDep, session: SessionDep):
    rows = (
        (
            await session.execute(
                select(UserBulletinSubscription)
                .where(
                    UserBulletinSubscription.user_id == auth.user_id,
                    UserBulletinSubscription.deleted_at.is_(None),
                )
                .order_by(UserBulletinSubscription.id)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [serializers.subscription_to_dict(s) for s in rows]}


class SubscriptionsPutBody(BaseModel):
    rules: list[SubscriptionCreate]


@subscriptions_router.put("")
async def replace_subscriptions(
    payload: SubscriptionsPutBody, auth: CurrentAuthDep, session: SessionDep
):
    """Snapshot-style replacement: soft-delete all existing, then insert."""
    existing = (
        (
            await session.execute(
                select(UserBulletinSubscription).where(
                    UserBulletinSubscription.user_id == auth.user_id,
                    UserBulletinSubscription.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in existing:
        row.deleted_at = now
        row.updated_by_device_id = auth.device_id
    created = []
    for rule in payload.rules:
        s = UserBulletinSubscription(
            user_id=auth.user_id,
            name=rule.name,
            orgs=rule.orgs,
            tags=rule.tags,
            mode=rule.mode,
            enabled=rule.enabled,
            created_by_device_id=auth.device_id,
            updated_by_device_id=auth.device_id,
        )
        session.add(s)
        created.append(s)
    await session.flush()
    return {"items": [serializers.subscription_to_dict(s) for s in created]}


@subscriptions_router.post("")
async def create_subscription(
    payload: SubscriptionCreate, auth: CurrentAuthDep, session: SessionDep
):
    subscription = UserBulletinSubscription(
        user_id=auth.user_id,
        name=payload.name,
        orgs=payload.orgs,
        tags=payload.tags,
        mode=payload.mode,
        enabled=payload.enabled,
        created_by_device_id=auth.device_id,
        updated_by_device_id=auth.device_id,
    )
    session.add(subscription)
    await session.flush()
    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="bulletin_subscription",
        entity_id=str(subscription.id),
        operation="upsert",
        device_id=auth.device_id,
    )
    return serializers.subscription_to_dict(subscription)


@subscriptions_router.patch("/{subscription_id}")
async def patch_subscription(
    subscription_id: int,
    payload: SubscriptionPatch,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    subscription = await _get_owned_subscription(
        session, auth.user_id, subscription_id
    )
    if subscription.revision != payload.base_revision:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "subscription_conflict",
                "server": serializers.subscription_to_dict(subscription),
            },
        )
    if payload.name is not None:
        subscription.name = payload.name
    if payload.orgs is not None:
        subscription.orgs = payload.orgs
    if payload.tags is not None:
        subscription.tags = payload.tags
    if payload.mode is not None:
        subscription.mode = payload.mode
    if payload.enabled is not None:
        subscription.enabled = payload.enabled
    subscription.revision += 1
    subscription.updated_by_device_id = auth.device_id

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="bulletin_subscription",
        entity_id=str(subscription.id),
        operation="upsert",
        device_id=auth.device_id,
    )
    return serializers.subscription_to_dict(subscription)


@subscriptions_router.delete(
    "/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_subscription(
    subscription_id: int,
    auth: CurrentAuthDep,
    session: SessionDep,
    base_revision: int | None = Query(default=None),
):
    subscription = await _get_owned_subscription(
        session, auth.user_id, subscription_id
    )
    # Spec classifies subscriptions as independent entities (delete wins,
    # natural merge), so base_revision stays OPTIONAL — but a client that
    # supplies it gets the same CAS guard as PATCH: a delete decided on a
    # stale read 409s instead of silently discarding a newer edit.
    if base_revision is not None and subscription.revision != base_revision:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "subscription_conflict",
                "server": serializers.subscription_to_dict(subscription),
            },
        )
    subscription.deleted_at = datetime.now(UTC)
    subscription.updated_by_device_id = auth.device_id
    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="bulletin_subscription",
        entity_id=str(subscription.id),
        operation="delete",
        device_id=auth.device_id,
    )


@states_router.get("")
async def get_states(
    auth: CurrentAuthDep,
    session: SessionDep,
    bulletin_ids: str = Query(min_length=1, max_length=4096),
):
    try:
        ids = [int(x) for x in bulletin_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bulletin_ids must be comma-separated integers",
        )
    if len(ids) > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="too many bulletin_ids (max 200)",
        )
    rows = (
        (
            await session.execute(
                select(UserBulletinState).where(
                    UserBulletinState.user_id == auth.user_id,
                    UserBulletinState.bulletin_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return {"items": [serializers.bulletin_state_to_dict(s) for s in rows]}


@states_router.put("/{bulletin_id}")
async def put_state(
    bulletin_id: int,
    payload: BulletinStatePut,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    bulletin = await session.get(Bulletin, bulletin_id)
    if bulletin is None:
        raise HTTPException(status_code=404, detail="bulletin not found")

    state = (
        await session.execute(
            select(UserBulletinState).where(
                UserBulletinState.user_id == auth.user_id,
                UserBulletinState.bulletin_id == bulletin_id,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        state = UserBulletinState(user_id=auth.user_id, bulletin_id=bulletin_id)
        session.add(state)
        await session.flush()

    now = datetime.now(UTC)
    changed_fields = []
    for field_name, merge_field, value, ts in (
        ("is_read", "read", payload.is_read, payload.read_updated_at),
        ("is_starred", "starred", payload.is_starred, payload.starred_updated_at),
        ("is_hidden", "hidden", payload.is_hidden, payload.hidden_updated_at),
    ):
        if ts is None:
            continue
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field_name} cannot be null",
            )
        # Column triplet is (is_read, read_updated_at, read_device_id) — the
        # merge helper needs the timestamp/device prefix, not the bool name.
        # Changelog keys off "merge applied", not "value flipped": even a
        # same-value win advances the field's merge metadata, and other
        # devices must learn about it or later equal-timestamp edits resolve
        # differently on different devices.
        if apply_field_boolean(
            state, field_name, merge_field, value, ts, auth.device_id, now
        ):
            changed_fields.append(field_name)
        if field_name == "is_read" and state.is_read and state.first_read_at is None:
            state.first_read_at = now

    if changed_fields:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type="bulletin_state",
            entity_id=str(bulletin_id),
            operation="upsert",
            payload={"fields": changed_fields},
            device_id=auth.device_id,
        )
    return serializers.bulletin_state_to_dict(state)


def apply_field_boolean(
    state, value_field: str, ts_prefix: str, value, ts, device_id, now
) -> bool:
    """Adapter for the spec's asymmetric column names (is_read vs
    read_updated_at): same clamp + newer-wins semantics as merge.apply_field."""
    effective_ts = clamp_ts(ts, now)
    stored_ts = getattr(state, f"{ts_prefix}_updated_at")
    if stored_ts is not None and effective_ts <= stored_ts:
        return False
    setattr(state, value_field, value)
    setattr(state, f"{ts_prefix}_updated_at", effective_ts)
    setattr(state, f"{ts_prefix}_device_id", device_id)
    return True
