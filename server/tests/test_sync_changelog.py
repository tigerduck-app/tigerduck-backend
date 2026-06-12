"""Tests for the change log service — especially the per-user commit
ordering guarantee (security review 1.1)."""

from __future__ import annotations

import asyncio

import pytest

from server.auth.models import User
from server.db import build_session_factory
from server.sync.changelog import RevisionExpired, append_change, read_changes
from server.sync.models import UserSyncState

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def make_user(session) -> User:
    user = User(student_id="B11015000")
    session.add(user)
    await session.flush()
    return user


async def test_append_assigns_increasing_revisions_and_bumps_state(
    db_session,
) -> None:
    user = await make_user(db_session)
    r1 = await append_change(
        db_session,
        user_id=user.id,
        entity_type="settings_document",
        entity_id="home_layout",
        operation="upsert",
        payload={"revision": 1},
    )
    r2 = await append_change(
        db_session,
        user_id=user.id,
        entity_type="assignment",
        entity_id="42",
        operation="upsert",
    )
    await db_session.commit()
    assert r2 > r1

    state = await db_session.get(UserSyncState, user.id)
    assert state is not None
    assert state.current_revision == r2


async def test_read_changes_pagination(db_session) -> None:
    user = await make_user(db_session)
    for i in range(5):
        await append_change(
            db_session,
            user_id=user.id,
            entity_type="assignment",
            entity_id=str(i),
            operation="upsert",
        )
    await db_session.commit()

    page1 = await read_changes(
        db_session, user_id=user.id, since_revision=0, limit=3
    )
    assert len(page1.changes) == 3
    assert page1.has_more is True

    page2 = await read_changes(
        db_session,
        user_id=user.id,
        since_revision=page1.returned_until_revision,
        limit=3,
    )
    assert len(page2.changes) == 2
    assert page2.has_more is False
    assert page2.returned_until_revision == page2.current_revision

    revisions = [c.revision for c in page1.changes + page2.changes]
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == 5


async def test_read_changes_empty_user(db_session) -> None:
    user = await make_user(db_session)
    await db_session.commit()
    result = await read_changes(
        db_session, user_id=user.id, since_revision=0, limit=10
    )
    assert result.changes == []
    assert result.current_revision == 0
    assert result.has_more is False


async def test_expired_revision_raises(db_session) -> None:
    user = await make_user(db_session)
    await append_change(
        db_session,
        user_id=user.id,
        entity_type="assignment",
        entity_id="1",
        operation="upsert",
    )
    state = await db_session.get(UserSyncState, user.id)
    state.compacted_revision = state.current_revision
    await db_session.commit()

    with pytest.raises(RevisionExpired) as exc_info:
        await read_changes(db_session, user_id=user.id, since_revision=0, limit=10)
    assert exc_info.value.min_available_revision == state.compacted_revision


async def test_uncommitted_append_is_invisible_to_readers(
    db_session, prepared_engine
) -> None:
    """The review-1.1 property: a reader can never see revision N+1 while
    revision N is still uncommitted, because current_revision (the read
    watermark) only advances inside the same transaction as the append —
    and per-user appends serialize on the user_sync_state row lock."""
    user = await make_user(db_session)
    committed = await append_change(
        db_session,
        user_id=user.id,
        entity_type="assignment",
        entity_id="1",
        operation="upsert",
    )
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    async with factory() as writer:
        # Uncommitted append in a second session...
        await append_change(
            writer,
            user_id=user.id,
            entity_type="assignment",
            entity_id="2",
            operation="upsert",
        )
        # ...is invisible: the reader's watermark still ends at `committed`.
        result = await read_changes(
            db_session, user_id=user.id, since_revision=0, limit=10
        )
        assert result.current_revision == committed
        assert [c.revision for c in result.changes] == [committed]
        await writer.rollback()


async def test_concurrent_appends_serialize_per_user(
    db_session, prepared_engine
) -> None:
    user = await make_user(db_session)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    async def one_append(entity_id: str) -> int:
        async with factory() as session:
            revision = await append_change(
                session,
                user_id=user.id,
                entity_type="assignment",
                entity_id=entity_id,
                operation="upsert",
            )
            await session.commit()
            return revision

    revisions = await asyncio.gather(*[one_append(str(i)) for i in range(8)])
    assert len(set(revisions)) == 8

    result = await read_changes(
        db_session, user_id=user.id, since_revision=0, limit=20
    )
    assert result.current_revision == max(revisions)
    assert [c.revision for c in result.changes] == sorted(revisions)
