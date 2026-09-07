"""Phase 4c: user-level bulletin dispatch via push_jobs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.auth.models import PushJob, User
from server.bulletins.models import Bulletin
from server.bulletins.taxonomy import CanonicalOrg, ContentTag
from server.db import build_session_factory
from server.bulletins.user_dispatch import dispatch_user_bulletins
from server.sync.models import (
    BulletinUserMatch,
    BulletinUserMatchRun,
    UserBulletinSubscription,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

ORG = CanonicalOrg.library.value
TAG = ContentTag.free_meal.value


def _bulletin(external_id="b-1", **kwargs):
    defaults = dict(
        external_id=external_id,
        title="原始標題",
        title_clean="圖書館免費餐券",
        summary="到圖書館領餐券",
        source_url=f"https://bulletin.ntust.edu.tw/{external_id}",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        processing_state="processed",
        canonical_org=ORG,
        content_tags=[TAG],
    )
    defaults.update(kwargs)
    return Bulletin(**defaults)


async def _user_with_sub(session, student_id="b11203058", **sub_kwargs):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    sub_defaults = dict(user_id=user.id, orgs=[], tags=[TAG], mode="AND")
    sub_defaults.update(sub_kwargs)
    sub = UserBulletinSubscription(**sub_defaults)
    session.add(sub)
    await session.flush()
    return user, sub


async def test_matched_user_gets_push_job(db_session, prepared_engine, test_settings):
    user, sub = await _user_with_sub(db_session)
    bulletin = _bulletin()
    db_session.add(bulletin)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    created = await dispatch_user_bulletins(factory, test_settings)
    assert created == 1

    match = (
        await db_session.execute(
            select(BulletinUserMatch).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert match.user_id == user.id
    assert match.subscription_id == sub.id
    assert match.pushed_at is not None
    assert match.push_job_id is not None

    job = await db_session.get(PushJob, match.push_job_id)
    assert job.channel == "bulletin"
    assert job.dedupe_key == f"bulletin:{bulletin.id}"
    assert job.payload["title"] == "圖書館免費餐券"
    assert job.payload["body"] == "到圖書館領餐券"
    assert job.payload["bulletin_id"] == bulletin.id

    # Run marker written; anonymous-flow stamp untouched.
    run = (await db_session.execute(select(BulletinUserMatchRun))).scalar_one()
    assert run.bulletin_id == bulletin.id
    await db_session.refresh(bulletin)
    assert bulletin.notified_at is None

    # Second pass is a no-op (run marker + pushed_at + dedupe).
    assert await dispatch_user_bulletins(factory, test_settings) == 0
    jobs = (await db_session.execute(select(PushJob))).scalars().all()
    assert len(jobs) == 1


async def test_non_matching_and_disabled_subscriptions(
    db_session, prepared_engine, test_settings
):
    await _user_with_sub(db_session, student_id="b11203051", tags=["scholarship"])
    await _user_with_sub(db_session, student_id="b11203052", enabled=False)
    await _user_with_sub(
        db_session, student_id="b11203053", deleted_at=datetime.now(UTC)
    )
    db_session.add(_bulletin())
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await dispatch_user_bulletins(factory, test_settings) == 0
    matches = (await db_session.execute(select(BulletinUserMatch))).scalars().all()
    assert matches == []
    # Run marker still written → bulletin not rescanned forever.
    run = (await db_session.execute(select(BulletinUserMatchRun))).scalar_one()
    assert run is not None


async def test_unprocessed_or_deleted_bulletins_ignored(
    db_session, prepared_engine, test_settings
):
    await _user_with_sub(db_session)
    db_session.add(_bulletin("b-pending", processing_state="pending"))
    db_session.add(_bulletin("b-deleted", is_deleted=True))
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await dispatch_user_bulletins(factory, test_settings) == 0
    runs = (await db_session.execute(select(BulletinUserMatchRun))).scalars().all()
    assert runs == []


async def test_crash_recovery_pushes_unstamped_match(
    db_session, prepared_engine, test_settings
):
    """A match row left with pushed_at NULL (crash between pass A and B)
    gets its push_job on the next tick even though the run marker exists."""
    user, sub = await _user_with_sub(db_session)
    bulletin = _bulletin()
    db_session.add(bulletin)
    await db_session.flush()
    db_session.add(BulletinUserMatchRun(bulletin_id=bulletin.id))
    db_session.add(
        BulletinUserMatch(
            bulletin_id=bulletin.id, user_id=user.id, subscription_id=sub.id
        )
    )
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await dispatch_user_bulletins(factory, test_settings) == 1
    match = (
        await db_session.execute(
            select(BulletinUserMatch).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert match.pushed_at is not None
    assert match.push_job_id is not None


async def test_dedupe_conflict_still_stamps_match(
    db_session, prepared_engine, test_settings
):
    """If the push_job already exists (re-run after partial commit), the
    match is stamped with the existing job instead of erroring."""
    user, sub = await _user_with_sub(db_session)
    bulletin = _bulletin()
    db_session.add(bulletin)
    await db_session.flush()
    existing = PushJob(
        user_id=user.id,
        dedupe_key=f"bulletin:{bulletin.id}",
        channel="bulletin",
        scenario="bulletin_matched",
        fire_at=datetime.now(UTC),
        payload={"title": "t", "body": "b"},
    )
    db_session.add(existing)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    created = await dispatch_user_bulletins(factory, test_settings)
    assert created == 0  # job already existed

    match = (
        await db_session.execute(
            select(BulletinUserMatch).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert match.push_job_id == existing.id
    assert match.pushed_at is not None
