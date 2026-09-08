"""Per-user holiday exceptions and how they reach a user's other devices.

The split under test: the holiday is school-wide and public, the exception
is user-scoped and synced. Anything that blurs those two shows up here.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from server.academic_calendar.models import AcademicHoliday
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import UserChangeLog

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def do_login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
        },
    )
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def seed_holiday(client, name_zh="中秋節", name_en="Mid-Autumn") -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        holiday = AcademicHoliday(
            name_zh=name_zh,
            name_en=name_en,
            start_date=date(2026, 9, 25),
            end_date=date(2026, 9, 27),
        )
        session.add(holiday)
        await session.commit()
        return holiday.id


async def test_override_requires_a_session(client) -> None:
    """The holiday feed is public; the exception is not."""
    holiday_id = await seed_holiday(client)
    response = await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}", json={"notify": True}
    )
    assert response.status_code == 401


async def test_set_and_read_back(client) -> None:
    holiday_id = await seed_holiday(client)
    login = await do_login(client)

    put = await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": True},
    )
    assert put.status_code == 200
    assert put.json() == {"holiday_id": holiday_id, "notify": True}

    listed = await client.get("/v3/sync/holiday-overrides", headers=bearer(login))
    assert listed.json()["overrides"] == [{"holiday_id": holiday_id, "notify": True}]


async def test_turning_it_back_off_updates_rather_than_deletes(client) -> None:
    """A delete would be indistinguishable from "never set" to a device
    that had not synced in between; storing false says what happened."""
    holiday_id = await seed_holiday(client)
    login = await do_login(client)
    await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": True},
    )
    off = await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": False},
    )
    assert off.status_code == 200

    listed = await client.get("/v3/sync/holiday-overrides", headers=bearer(login))
    assert listed.json()["overrides"] == [{"holiday_id": holiday_id, "notify": False}]


async def test_unknown_holiday_is_rejected(client) -> None:
    login = await do_login(client)
    response = await client.put(
        "/v3/sync/holiday-overrides/999999",
        headers=bearer(login),
        json={"notify": True},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "holiday_not_found"


async def test_change_is_appended_to_the_changelog(client) -> None:
    """This is what carries the choice to the user's other devices, and the
    changelog gates entity_type with a CHECK — so a missing enum value would
    fail the commit rather than silently skip the sync."""
    holiday_id = await seed_holiday(client)
    login = await do_login(client)
    await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": True},
    )

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        entries = (
            (
                await session.execute(
                    select(UserChangeLog).where(
                        UserChangeLog.entity_type == "holiday_override"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(entries) == 1
    assert entries[0].entity_id == str(holiday_id)
    assert entries[0].operation == "upsert"


async def test_full_sync_carries_the_override(client) -> None:
    """A device signing in fresh has to receive the exception without
    replaying the whole changelog."""
    holiday_id = await seed_holiday(client)
    login = await do_login(client)
    await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": True},
    )

    snapshot = await client.get("/v3/sync/full", headers=bearer(login))
    assert snapshot.status_code == 200
    assert snapshot.json()["holiday_overrides"] == [
        {"holiday_id": holiday_id, "notify": True}
    ]


async def test_deleting_the_holiday_removes_the_override(client) -> None:
    """An exception means "notify me anyway on *this* holiday" and has no
    meaning once the holiday is gone — the FK cascade is what guarantees a
    removed holiday leaves nothing behind."""
    holiday_id = await seed_holiday(client)
    login = await do_login(client)
    await client.put(
        f"/v3/sync/holiday-overrides/{holiday_id}",
        headers=bearer(login),
        json={"notify": True},
    )

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        holiday = await session.get(AcademicHoliday, holiday_id)
        await session.delete(holiday)
        await session.commit()

    listed = await client.get("/v3/sync/holiday-overrides", headers=bearer(login))
    assert listed.json()["overrides"] == []
