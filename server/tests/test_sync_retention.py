"""Tests for changelog retention + compacted_revision tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from server.auth.models import User
from server.config import Settings
from server.db import build_session_factory
from server.sync.changelog import RevisionExpired, append_change, read_changes
from server.sync.models import UserChangeLog, UserSyncState
from server.sync.retention import purge_expired_changelog

pytestmark = pytest.mark.asyncio(loop_scope="session")


def retention_settings() -> Settings:
    return Settings(sync_changelog_retention_days=30)


async def seed_user_with_changes(session, n: int) -> tuple[User, list[int]]:
    user = User(student_id="B11015000")
    session.add(user)
    await session.flush()
    revisions = []
    for i in range(n):
        revisions.append(
            await append_change(
                session,
                user_id=user.id,
                entity_type="assignment",
                entity_id=str(i),
                operation="upsert",
            )
        )
    await session.commit()
    return user, revisions


async def backdate(session, revisions: list[int], days: int) -> None:
    await session.execute(
        update(UserChangeLog)
        .where(UserChangeLog.revision.in_(revisions))
        .values(created_at=datetime.now(UTC) - timedelta(days=days))
    )
    await session.commit()


async def test_purge_deletes_old_and_updates_compacted(
    db_session, prepared_engine
) -> None:
    user, revisions = await seed_user_with_changes(db_session, 5)
    old, recent = revisions[:3], revisions[3:]
    await backdate(db_session, old, days=31)

    factory = build_session_factory(prepared_engine)
    deleted = await purge_expired_changelog(factory, retention_settings())
    assert deleted == 3

    remaining = (
        (
            await db_session.execute(
                select(UserChangeLog.revision).where(
                    UserChangeLog.user_id == user.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(remaining) == recent

    state = await db_session.get(UserSyncState, user.id)
    await db_session.refresh(state)
    assert state.compacted_revision == old[-1]
    assert state.current_revision == revisions[-1]

    # A client that slept too long must now be told to full-sync...
    with pytest.raises(RevisionExpired):
        await read_changes(db_session, user_id=user.id, since_revision=0, limit=10)

    # ...while a client at the compaction boundary still reads fine.
    page = await read_changes(
        db_session, user_id=user.id, since_revision=old[-1], limit=10
    )
    assert [c.revision for c in page.changes] == recent


async def test_purge_leaves_fresh_entries_alone(
    db_session, prepared_engine
) -> None:
    user, revisions = await seed_user_with_changes(db_session, 3)

    factory = build_session_factory(prepared_engine)
    deleted = await purge_expired_changelog(factory, retention_settings())
    assert deleted == 0

    state = await db_session.get(UserSyncState, user.id)
    await db_session.refresh(state)
    assert state.compacted_revision == 0
    assert state.current_revision == revisions[-1]
