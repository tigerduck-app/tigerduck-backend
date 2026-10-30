"""The sync trigger's pace: a background push asks every device of the user
to sync, and Apple advises two or three of those an hour at most (#107)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import PushDelivery, PushJob, User
from server.db import build_session_factory
from server.push.sync_trigger import MIN_GAP, enqueue_sync_trigger

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user(session):
    user = User(student_id="b11203058")
    session.add(user)
    await session.commit()
    return user


async def _enqueue(prepared_engine, user, source_device_id=None):
    """One writer's transaction, committed."""
    async with build_session_factory(prepared_engine)() as session:
        job_id = await enqueue_sync_trigger(
            session, user.id, source_device_id=source_device_id
        )
        await session.commit()
    return job_id


async def _triggers(session, user):
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user.id, PushJob.scenario == "sync_trigger")
                .order_by(PushJob.fire_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_changes_while_a_trigger_waits_ride_on_it(db_session, prepared_engine):
    user = await _user(db_session)

    first = await _enqueue(prepared_engine, user, "iphone")
    second = await _enqueue(prepared_engine, user, "ipad")

    assert first is not None and second is None
    [trigger] = await _triggers(db_session, user)
    # Each device made a change the other has to fetch.
    assert trigger.payload["source_device_id"] is None


async def test_a_change_after_a_trigger_went_out_waits_out_the_gap(
    db_session, prepared_engine
):
    """The old five-minute key let one through every five minutes, twelve an
    hour to every device."""
    user = await _user(db_session)
    await _enqueue(prepared_engine, user)
    [sent] = await _triggers(db_session, user)
    sent.status = "sent"
    await db_session.commit()

    await _enqueue(prepared_engine, user)
    await _enqueue(prepared_engine, user)

    triggers = await _triggers(db_session, user)
    assert [t.status for t in triggers] == ["sent", "pending"]
    assert triggers[1].fire_at == sent.fire_at + MIN_GAP


@pytest.mark.parametrize("going_out", ["claimed", "retrying"])
async def test_a_change_never_rides_on_a_trigger_going_out(
    db_session, prepared_engine, going_out
):
    """A claimed trigger may reach a device before this change commits, and
    one retrying has reached some already."""
    user = await _user(db_session)
    await _enqueue(prepared_engine, user)
    [first] = await _triggers(db_session, user)
    factory = build_session_factory(prepared_engine)
    async with factory() as claim:
        if going_out == "claimed":
            await claim.execute(
                select(PushJob).where(PushJob.id == first.id).with_for_update()
            )
        else:
            claim.add(
                PushDelivery(
                    push_job_id=first.id,
                    user_id=user.id,
                    provider="apns",
                    token_kind="standard",
                    token_hash="hash",
                    status="sent",
                    sent_at=datetime.now(UTC),
                )
            )
            await claim.commit()

        await _enqueue(prepared_engine, user)

    triggers = await _triggers(db_session, user)
    assert len(triggers) == 2
    assert triggers[1].fire_at == first.fire_at + MIN_GAP


async def test_the_first_trigger_goes_at_once(db_session, prepared_engine):
    user = await _user(db_session)
    before = datetime.now(UTC)

    await _enqueue(prepared_engine, user)

    [trigger] = await _triggers(db_session, user)
    assert before <= trigger.fire_at <= datetime.now(UTC) + timedelta(seconds=1)
