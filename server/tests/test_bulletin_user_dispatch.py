"""Phase 4c: user-level bulletin dispatch via push_jobs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, literal, select

from server.auth.models import PushJob, User, UserDevice
from server.bulletins import user_dispatch
from server.bulletins.models import Bulletin
from server.bulletins.taxonomy import CanonicalOrg, ContentTag
from server.bulletins.user_dispatch import (
    _match_new_bulletins,
    dispatch_user_bulletins,
)
from server.db import build_session_factory, session_scope
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


async def _device(session, user, client_device_id="dev-1", **kwargs):
    device = UserDevice(
        user_id=user.id, client_device_id=client_device_id, platform="ios", **kwargs
    )
    session.add(device)
    await session.flush()
    return device


async def _user_with_sub(session, student_id="b11203058", **sub_kwargs):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    device = await _device(session, user, client_device_id=f"dev-{student_id}")
    sub_defaults = dict(user_id=user.id, device_id=device.id, orgs=[], tags=[TAG], mode="AND")
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


async def test_push_job_targets_only_the_matching_devices(
    db_session, prepared_engine, test_settings
):
    """Rules belong to a device, so a hit on the phone's rule must not
    reach the iPad, whose own rule does not match."""
    user, phone_sub = await _user_with_sub(db_session)
    ipad = await _device(db_session, user, client_device_id="ipad-1", )
    db_session.add(
        UserBulletinSubscription(
            user_id=user.id, device_id=ipad.id, orgs=[], tags=["scholarship"], mode="AND"
        )
    )
    db_session.add(_bulletin())
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await dispatch_user_bulletins(factory, test_settings) == 1

    match = (await db_session.execute(select(BulletinUserMatch))).scalar_one()
    job = await db_session.get(PushJob, match.push_job_id)
    assert job.payload["target_device_ids"] == [str(phone_sub.device_id)]


async def _subscribe_users(session, count: int) -> None:
    """`count` users, each with an iPhone whose rule matches every TAG
    bulletin. Postgres generates them: built in Python, the rows take
    seconds."""
    await session.execute(
        insert(User).from_select(
            ["id"],
            select(func.gen_random_uuid()).select_from(func.generate_series(1, count)),
        )
    )
    device = {
        "id": func.gen_random_uuid(),
        "user_id": User.id,
        "client_device_id": literal("iphone"),
        "platform": literal("ios"),
    }
    await session.execute(
        insert(UserDevice).from_select(list(device), select(*device.values()))
    )
    subscription = {
        "user_id": UserDevice.user_id,
        "device_id": UserDevice.id,
        "orgs": literal([], UserBulletinSubscription.orgs.type),
        "tags": literal([TAG], UserBulletinSubscription.tags.type),
        "mode": literal("AND"),
    }
    await session.execute(
        insert(UserBulletinSubscription).from_select(
            list(subscription), select(*subscription.values())
        )
    )


async def test_a_bulletin_matches_more_users_than_one_insert_binds(
    db_session, prepared_engine, test_settings
):
    """asyncpg allows 32,767 bind parameters per statement and a match row
    binds 5. Filed in one insert, the matches of a bulletin for 6,554 users
    failed the tick, every tick, and with it the pushes of every bulletin.

    Runs the match pass alone: pushing to that many users takes seconds.
    """
    await _subscribe_users(db_session, 6_554)
    bulletin = _bulletin()
    db_session.add(bulletin)
    await db_session.commit()

    async with session_scope(build_session_factory(prepared_engine)) as session:
        await _match_new_bulletins(session, test_settings, datetime.now(UTC))

    count = select(func.count()).select_from(BulletinUserMatch)
    assert await db_session.scalar(count) == 6_554
    assert await db_session.get(BulletinUserMatchRun, bulletin.id) is not None


async def _pushed(session) -> set[int]:
    """The bulletins whose matches have their push job."""
    rows = await session.execute(
        select(BulletinUserMatch.bulletin_id).where(
            BulletinUserMatch.pushed_at.is_not(None)
        )
    )
    return set(rows.scalars())


async def test_a_bulletin_that_fails_to_match_holds_up_no_other(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A tick matches bulletins oldest first, in one transaction with the
    pushes. One whose matching raises is rolled back alone and retried on
    the next tick; the bulletins after it, and a match already filed, are
    pushed."""
    user, sub = await _user_with_sub(db_session)
    filed = _bulletin("b-0")
    failing = _bulletin("b-1", content_tags=[TAG, ContentTag.exam.value])
    later = _bulletin("b-2")
    db_session.add_all([filed, failing, later])
    await db_session.flush()
    # Matched by an earlier tick and not pushed yet.
    db_session.add(BulletinUserMatchRun(bulletin_id=filed.id))
    db_session.add(
        BulletinUserMatch(bulletin_id=filed.id, user_id=user.id, subscription_id=sub.id)
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    real_rule_hits = user_dispatch.rule_hits

    def rule_hits(rule, *, canonical_org, content_tags):
        if ContentTag.exam.value in content_tags:
            raise RuntimeError("matching failed")
        return real_rule_hits(
            rule, canonical_org=canonical_org, content_tags=content_tags
        )

    monkeypatch.setattr(user_dispatch, "rule_hits", rule_hits)
    assert await dispatch_user_bulletins(factory, test_settings) == 2
    assert await _pushed(db_session) == {filed.id, later.id}
    assert await db_session.get(BulletinUserMatchRun, failing.id) is None

    monkeypatch.undo()
    assert await dispatch_user_bulletins(factory, test_settings) == 1
    assert await _pushed(db_session) == {filed.id, failing.id, later.id}


async def test_signed_out_device_rules_are_not_matched(
    db_session, prepared_engine, test_settings
):
    user, sub = await _user_with_sub(db_session)
    device = await db_session.get(UserDevice, sub.device_id)
    device.deleted_at = datetime.now(UTC)
    db_session.add(_bulletin())
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await dispatch_user_bulletins(factory, test_settings) == 0
    assert (await db_session.execute(select(BulletinUserMatch))).scalars().all() == []
