"""Round-trip and constraint tests for settings / bulletin / sync-infra tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.auth.models import User
from server.bulletins.models import Bulletin
from server.sync.models import (
    BulletinUserMatch,
    UserBulletinState,
    UserBulletinSubscription,
    UserChangeLog,
    UserSettingsDocument,
    UserSyncState,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def make_user(db_session) -> User:
    user = User(student_id="B11015000")
    db_session.add(user)
    await db_session.flush()
    return user


async def make_bulletin(db_session) -> Bulletin:
    bulletin = Bulletin(
        external_id="ext-1",
        title="期末考公告",
        source_url="https://bulletin.ntust.edu.tw/x",
        canonical_org="教務處",
    )
    db_session.add(bulletin)
    await db_session.flush()
    return bulletin


async def test_settings_document_round_trip_and_unique(db_session) -> None:
    user = await make_user(db_session)
    db_session.add(
        UserSettingsDocument(
            user_id=user.id,
            namespace="home_layout",
            document={"sections": ["schedule", "assignments"]},
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UserSettingsDocument).where(
                UserSettingsDocument.user_id == user.id
            )
        )
    ).scalar_one()
    assert row.revision == 1
    assert row.schema_version == 1

    # Second active document in the same namespace violates the partial
    # unique index.
    db_session.add(
        UserSettingsDocument(
            user_id=user.id, namespace="home_layout", document={}
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_bulletin_subscription_and_state_round_trip(db_session) -> None:
    user = await make_user(db_session)
    bulletin = await make_bulletin(db_session)

    db_session.add(
        UserBulletinSubscription(
            user_id=user.id, name="教務處", orgs=["教務處"], tags=["修課"]
        )
    )
    db_session.add(
        UserBulletinState(
            user_id=user.id,
            bulletin_id=bulletin.id,
            is_read=True,
            read_updated_at=datetime.now(UTC),
            first_read_at=datetime.now(UTC),
        )
    )
    db_session.add(
        BulletinUserMatch(user_id=user.id, bulletin_id=bulletin.id)
    )
    await db_session.commit()

    sub = (
        await db_session.execute(select(UserBulletinSubscription))
    ).scalar_one()
    assert sub.mode == "AND"
    assert sub.enabled is True
    assert sub.revision == 1

    # (user, bulletin) state must be unique.
    db_session.add(
        UserBulletinState(user_id=user.id, bulletin_id=bulletin.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_change_log_and_sync_state(db_session) -> None:
    user = await make_user(db_session)
    db_session.add(UserSyncState(user_id=user.id))
    entry = UserChangeLog(
        user_id=user.id,
        entity_type="settings_document",
        entity_id="home_layout",
        operation="upsert",
        payload={"revision": 1},
    )
    db_session.add(entry)
    await db_session.commit()

    state = await db_session.get(UserSyncState, user.id)
    assert state is not None
    assert state.current_revision == 0
    assert state.compacted_revision == 0
    assert entry.revision >= 1

    # current_revision >= compacted_revision is enforced.
    state.compacted_revision = 10
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_change_log_rejects_unknown_entity_type(db_session) -> None:
    user = await make_user(db_session)
    db_session.add(
        UserChangeLog(
            user_id=user.id,
            entity_type="push_job",  # deliberately NOT changelog-able
            entity_id="1",
            operation="upsert",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
