"""The silent push that asks a user's devices to sync after the server
changed their data on its own: the sync executor's writes, and an
assignment found submitted by the push pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import PushJob


async def enqueue_sync_trigger(
    session: AsyncSession, user_id: uuid.UUID, *, source: str | None = None
) -> None:
    """Tell the user's devices to sync, at most once per five minutes.

    `source` gives a writer that commits after the list sync a key of its
    own: the list sync's trigger for the same five minutes may already have
    gone out, and a device that synced on it did so before this writer's
    change existed.
    """
    now = datetime.now(UTC)
    dedupe_key = f"sync_trigger:{user_id}:{int(now.timestamp()) // 300}"
    if source is not None:
        dedupe_key = f"{dedupe_key}:{source}"
    stmt = (
        pg_insert(PushJob)
        .values(
            user_id=user_id,
            dedupe_key=dedupe_key,
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={"kind": "sync_trigger", "source_device_id": None},
        )
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)
