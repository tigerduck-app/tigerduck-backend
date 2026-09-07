"""Change log append/read service.

THE ordering rule (security review 1.1): `user_change_log.revision` is a
global BIGSERIAL, and sequence numbers are handed out at INSERT time — NOT
in commit order. Naively, a reader could observe revision N+1 committed
while N is still in flight, store `since_revision = N+1`, and permanently
skip N once it commits.

The fix has two halves, both implemented here:

1. Per-user appends serialize: `append_change` locks the user's
   `user_sync_state` row (FOR UPDATE) before inserting the log row, and the
   lock is held until the caller's transaction commits. So for any single
   user, revisions are also committed in increasing order.
2. Reads are watermarked: `read_changes` only returns rows with
   `revision <= user_sync_state.current_revision`, which is bumped in the
   same transaction as the append. Whatever the global sequence does,
   a user's watermark only ever advances over fully committed rows.

Cross-user revision interleaving is irrelevant — every read is scoped to
one user_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.models import UserChangeLog, UserSyncState


class RevisionExpired(Exception):
    """since_revision predates the oldest retained changelog entry —
    the client must full-sync (HTTP 410 at the API layer)."""

    def __init__(self, min_available_revision: int, current_revision: int) -> None:
        super().__init__(
            f"revision expired; min available {min_available_revision}"
        )
        self.min_available_revision = min_available_revision
        self.current_revision = current_revision


@dataclass(frozen=True)
class ChangeEntry:
    revision: int
    entity_type: str
    entity_id: str
    operation: str
    payload: dict | None


@dataclass(frozen=True)
class ChangePage:
    changes: list[ChangeEntry]
    current_revision: int
    returned_until_revision: int
    has_more: bool


async def lock_sync_state(
    session: AsyncSession, user_id: uuid.UUID
) -> UserSyncState:
    """Ensure the user's sync-state row exists and lock it (FOR UPDATE).

    The lock is held until the transaction commits, serializing all of the
    user's changelog appends. Bulk writers (initial upload) call this once
    and pass the row to `append_change` to avoid re-locking per entity.
    """
    await session.execute(
        pg_insert(UserSyncState)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    return (
        await session.execute(
            select(UserSyncState)
            .where(UserSyncState.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one()


async def append_change(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: str,
    operation: str,
    payload: dict | None = None,
    device_id: uuid.UUID | None = None,
    locked_state: UserSyncState | None = None,
) -> int:
    """Append one changelog entry inside the caller's transaction.

    Payload must contain routing hints only (entity ids, field names,
    revision pointers) — never documents or sensitive data.

    `locked_state`: pass the row from `lock_sync_state` when appending many
    entries in one transaction; the lock is already held, so re-acquiring
    it per entry would just add round-trips.
    """
    state = locked_state or await lock_sync_state(session, user_id)

    entry = UserChangeLog(
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        payload=payload,
        device_id=device_id,
    )
    session.add(entry)
    await session.flush()

    state.current_revision = entry.revision
    return entry.revision


async def read_changes(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    since_revision: int,
    limit: int,
) -> ChangePage:
    state = await session.get(UserSyncState, user_id)
    current = state.current_revision if state else 0
    compacted = state.compacted_revision if state else 0

    if since_revision < compacted:
        raise RevisionExpired(
            min_available_revision=compacted, current_revision=current
        )

    rows = (
        (
            await session.execute(
                select(UserChangeLog)
                .where(
                    UserChangeLog.user_id == user_id,
                    UserChangeLog.revision > since_revision,
                    UserChangeLog.revision <= current,
                )
                .order_by(UserChangeLog.revision)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    changes = [
        ChangeEntry(
            revision=r.revision,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            operation=r.operation,
            payload=r.payload,
        )
        for r in rows
    ]
    returned_until = changes[-1].revision if has_more and changes else current
    return ChangePage(
        changes=changes,
        current_revision=current,
        returned_until_revision=returned_until,
        has_more=has_more,
    )
