"""The portal's Course announcements page (`portal/app/routes/course_announcements.py`)
and the review items the Course changes routes approve and record, against the
tables the backend migrates. In the server suite for the reason
test_portal_sync_job_retry.py gives. Every announcement here is invented."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes import course_announcements as announcement_routes  # noqa: E402
from portal.app.routes.course_announcements import (  # noqa: E402
    approve_item,
    get_announcement,
    list_announcements,
    list_reviews,
    pending_count,
    reject_item,
)
from portal.app.routes.course_changes import (  # noqa: E402
    create_change,
    delete_change,
    update_change,
)
from server.academic_calendar.models import CourseChange, SemesterTerm  # noqa: E402
from server.auth.models import User  # noqa: E402
from server.course_announcements.ingest import store_announcements  # noqa: E402
from server.course_announcements.models import (  # noqa: E402
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
)
from server.sync.models import UserCourse  # noqa: E402
from server.syncjobs.moodle_client import FetchedDiscussion  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
COURSE = "CS2001301"
# Tuesday periods 3-4 and Thursday 2-3.
_DS = {"2": ["3", "4"], "4": ["2", "3"]}
# Tue Nov 17 and Thu Nov 19, 2026.
TUE, THU = "2026-11-17", "2026-11-19"


def _request(actor: str = "ops@example.com", path: str = "/api/course-announcements/x"):
    settings = SimpleNamespace(
        course_announcements_auto_apply=False, moodle_base_url="https://moodle.example"
    )
    return SimpleNamespace(
        state=SimpleNamespace(actor=actor),
        url=SimpleNamespace(path=path),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session):
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def term(db_session):
    """Term 1151 with one student of CS2001301."""
    user = User(student_id="b1")
    db_session.add_all(
        [
            SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8)),
            user,
        ]
    )
    await db_session.flush()
    db_session.add(
        UserCourse(
            user_id=user.id,
            semester="1151",
            course_key=f"client:1151:{COURSE}",
            course_no=COURSE,
            course_name="資料結構",
            schedule_json=_DS,
            classroom="TR-412",
        )
    )
    await db_session.commit()


def _change(kind: str = "cancelled", day: str = TUE, **fields) -> dict:
    """An item's change in `course_changes` column names, as the LLM returns it."""
    return {
        "kind": kind,
        "mode": "day",
        "start_date": day,
        "end_date": day,
        "excluded_weekdays": [],
        "periods": None,
        "room": None,
        "start_time": None,
        "end_time": None,
        "note_zh": None,
        **fields,
    }


async def _announcement(
    db_session,
    key: str = "101-901",
    *,
    subject: str = "11/17 停課",
    modified: datetime = POSTED,
    codes: tuple[str, ...] = (COURSE,),
    route: str = "sent",
    posted: datetime = POSTED,
) -> CourseAnnouncement:
    classified = route != "unclassified"
    row = CourseAnnouncement(
        id=key,
        term_code="1151",
        moodle_course_id=7001,
        course_codes=list(codes),
        raw_subject=None if classified else subject,
        raw_message=None if classified else "<p>Invented notice</p>",
        subject=subject,
        text=f"{subject}\n\n同學好：{subject}，請互相轉告。" if classified else None,
        posted_at=posted,
        modified_at=modified,
        group_id=-1,
        visible_from=None,
        has_attachment=False,
        classifier_release="model-test" if classified else None,
        scores={"class_cancel": 0.97, "class_change": 0.2} if classified else None,
        labels=["class_cancel"] if classified else None,
        route=route,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _extraction(
    db_session, announcement: CourseAnnouncement, *, status: str = "done", model: str = "m1",
    raw: dict | None = None,
) -> CourseAnnouncementExtraction:
    done = status == "done"
    row = CourseAnnouncementExtraction(
        announcement_id=announcement.id,
        version=announcement.modified_at,
        model=model if done else None,
        raw_output=raw if raw is not None else ({"course_changes": []} if done else None),
        certain=True if done else None,
        status=status,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _item(
    db_session, extraction, payload: dict | None = None, *, action: str | None = "add",
    state: str = "pending", kind: str = "course_change", course_no: str | None = COURSE,
    change_id: int | None = None, checks: dict | None = None,
) -> CourseAnnouncementItem:
    row = CourseAnnouncementItem(
        extraction_id=extraction.id,
        course_no=course_no,
        kind=kind,
        action=action,
        payload=payload if payload is not None else _change(),
        course_change_id=change_id,
        evidence="11/17 停課",
        checks=checks,
        passes_checks=False,
        state=state,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _applied_change(db_session, kind: str = "cancelled", day: str = TUE, **fields) -> int:
    row = CourseChange(
        term_code="1151",
        course_no=COURSE,
        kind=kind,
        mode="day",
        start_date=date.fromisoformat(day),
        end_date=date.fromisoformat(day),
        excluded_weekdays=[],
        created_by="course-announcements",
        updated_by="course-announcements",
        **fields,
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


def _body(response) -> dict:
    return json.loads(response.body)


async def _item_row(pool, item_id: int):
    return await pool.fetchrow(
        "SELECT state, course_change_id, decided_by, reason FROM course_announcement_items"
        " WHERE id = $1",
        item_id,
    )


async def _audit(pool) -> list[tuple[str, dict]]:
    rows = await pool.fetch("SELECT action, detail FROM admin_audit_log ORDER BY id")
    return [(r["action"], json.loads(r["detail"]) if r["detail"] else {}) for r in rows]


# --- Approve, reject, re-extract --------------------------------------------


async def test_approving_an_add_item_writes_the_change_with_its_source(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 200, _body(response)
    change_id = _body(response)["change_id"]
    change = await pool.fetchrow("SELECT * FROM course_changes WHERE id = $1", change_id)
    assert (change["kind"], change["start_date"], change["source_announcement_id"]) == (
        "cancelled", date(2026, 11, 17), "101-901",
    )
    assert change["created_by"] == "ops@example.com"
    assert tuple(await _item_row(pool, item.id)) == ("approved", change_id, "ops@example.com", None)
    assert await _audit(pool) == [
        (
            "course_change.create",
            {"change_id": change_id, "term": "1151", "course_no": COURSE,
             "kind": "cancelled", "item_id": item.id},
        )
    ]


async def test_approving_twice_writes_the_change_once(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    first, second = await asyncio.gather(
        approve_item(item.id, _request(), pool=pool),
        approve_item(item.id, _request("lead@example.com"), pool=pool),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 1
    assert [a for a, _ in await _audit(pool)] == ["course_change.create"]


async def test_an_item_a_newer_version_replaced_cannot_be_approved(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()
    edited = FetchedDiscussion(
        discussion_id=101, post_id=901, subject="11/17 照常上課", message="<p>Invented</p>",
        created_at=POSTED, modified_at=POSTED + timedelta(hours=1), group_id=-1,
        visible_from=None, has_attachment=False, pinned=False, starred=False,
    )
    await store_announcements(
        db_session, term_code="1151", moodle_course_id=7001, course_codes=[COURSE],
        discussions=[edited],
    )
    await db_session.commit()

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 409
    assert (await _item_row(pool, item.id))["state"] == "superseded"
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0
    assert await _audit(pool) == []


async def test_an_item_a_later_extraction_of_its_version_replaced_cannot_be_approved(
    term, pool, db_session
):
    announcement = await _announcement(db_session)
    old = await _item(db_session, await _extraction(db_session, announcement, model="m1"))
    await _item(db_session, await _extraction(db_session, announcement, model="m2"))
    await db_session.commit()

    response = await approve_item(old.id, _request(), pool=pool)

    assert response.status_code == 409
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0


async def test_a_rejected_item_keeps_its_reason_and_cannot_be_approved(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    rejected = await reject_item(
        item.id, _request(), pool=pool, body={"reason": "  The date is a typo.  "}
    )
    approved = await approve_item(item.id, _request(), pool=pool)

    assert rejected.status_code == 200
    assert approved.status_code == 409
    assert tuple(await _item_row(pool, item.id)) == (
        "rejected", None, "ops@example.com", "The date is a typo.",
    )
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0
    # The reason is typed text, so the audit row leaves it out.
    assert await _audit(pool) == [("course_announcement.reject", {"item_id": item.id})]


async def test_reject_refuses_a_reason_postgres_cannot_hold(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    for reason in ("a\x00b", "x" * 501, 5):
        response = await reject_item(item.id, _request(), pool=pool, body={"reason": reason})
        assert response.status_code == 400
    assert (await _item_row(pool, item.id))["state"] == "pending"


async def test_an_add_that_clashes_is_refused_and_stays_pending(term, pool, db_session):
    existing = await _applied_change(db_session)
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 409
    assert _body(response)["day"] == TUE
    assert (await _item_row(pool, item.id))["state"] == "pending"
    assert await pool.fetchval("SELECT array_agg(id) FROM course_changes") == [existing]


async def test_approving_a_replace_item_updates_the_change(term, pool, db_session):
    change_id = await _applied_change(db_session, "room", room="IB-201")
    announcement = await _announcement(db_session)
    wrote = await _item(
        db_session, await _extraction(db_session, announcement, model="m1"),
        _change("room", room="IB-201"), state="applied", change_id=change_id,
    )
    item = await _item(
        db_session, await _extraction(db_session, announcement),
        _change("room", room="IB-302"), action="replace", change_id=change_id,
    )
    await db_session.commit()

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 200, _body(response)
    room = await pool.fetchval("SELECT room FROM course_changes WHERE id = $1", change_id)
    assert room == "IB-302"
    assert tuple(await _item_row(pool, item.id))[:2] == ("approved", change_id)
    assert (await _item_row(pool, wrote.id))["state"] == "edited"
    assert [a for a, _ in await _audit(pool)] == ["course_change.update"]


@pytest.mark.parametrize(
    ("note_zh", "note_en"), [("改為線上上課", "Online class"), ("改為實體上課", None)]
)
async def test_a_replace_keeps_the_english_note_while_the_chinese_one_stays(
    term, pool, db_session, note_zh, note_en
):
    change_id = await _applied_change(
        db_session, "note", note_zh="改為線上上課", note_en="Online class"
    )
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement),
        _change("note", periods=["3"], note_zh=note_zh, note_en=None), action="replace",
        change_id=change_id,
    )
    await db_session.commit()

    listed = _body(await list_reviews("1151", _request(), pool=pool))["items"]
    response = await approve_item(item.id, _request(), pool=pool)

    assert listed[0]["payload"]["note_en"] == note_en
    assert response.status_code == 200, _body(response)
    row = await pool.fetchrow(
        "SELECT note_zh, note_en, periods FROM course_changes WHERE id = $1", change_id
    )
    assert tuple(row) == (note_zh, note_en, ["3"])


async def test_approving_a_replace_keeps_a_translation_saved_meanwhile(
    term, pool, db_session, monkeypatch
):
    change_id = await _applied_change(
        db_session, "note", note_zh="改為線上上課", note_en="Online class"
    )
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement),
        _change("note", periods=["3"], note_zh="改為線上上課", note_en=None), action="replace",
        change_id=change_id,
    )
    await db_session.commit()
    replace_change = announcement_routes.replace_change

    async def translated_meanwhile(*args, **kwargs):
        # Another operator saves a translation after the approval read the item.
        await pool.execute(
            "UPDATE course_changes SET note_en = 'Class moves online' WHERE id = $1", change_id
        )
        return await replace_change(*args, **kwargs)

    monkeypatch.setattr(announcement_routes, "replace_change", translated_meanwhile)

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 200, _body(response)
    note_en = await pool.fetchval("SELECT note_en FROM course_changes WHERE id = $1", change_id)
    assert note_en == "Class moves online"


async def test_approving_a_remove_item_deletes_the_change_and_marks_its_add(
    term, pool, db_session
):
    change_id = await _applied_change(db_session)
    announcement = await _announcement(db_session)
    first = await _extraction(db_session, announcement, model="m1")
    added = await _item(db_session, first, state="applied", change_id=change_id)
    later = await _extraction(db_session, announcement, model="m2")
    remove = await _item(db_session, later, action="remove", change_id=change_id)
    other = await _announcement(db_session, "102-902")
    replace = await _item(
        db_session, await _extraction(db_session, other), _change(day=THU), action="replace",
        change_id=change_id,
    )
    await db_session.commit()

    response = await approve_item(remove.id, _request(), pool=pool)

    assert response.status_code == 200, _body(response)
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0
    assert tuple(await _item_row(pool, remove.id))[:2] == ("approved", None)
    assert tuple(await _item_row(pool, added.id))[:2] == ("removed", None)
    assert tuple(await _item_row(pool, replace.id))[:2] == ("superseded", None)
    assert [a for a, _ in await _audit(pool)] == ["course_change.delete"]


async def test_a_replace_whose_change_is_gone_cannot_be_approved(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement), action="replace"
    )
    await db_session.commit()

    response = await approve_item(item.id, _request(), pool=pool)

    assert response.status_code == 409
    assert (await _item_row(pool, item.id))["state"] == "pending"


async def test_approving_a_replace_does_not_wait_for_a_store_reading_its_change(
    term, pool, db_session
):
    """A store reads the course's sourced changes FOR KEY SHARE; a replace
    locks its change FOR NO KEY UPDATE, which that lock lets through."""
    change_id = await _applied_change(db_session, "room", room="IB-201")
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement),
        _change("room", room="IB-302"), action="replace", change_id=change_id,
    )
    await db_session.commit()

    async with pool.acquire() as store, store.transaction():
        await store.execute(
            "SELECT 1 FROM course_changes WHERE id = $1 FOR KEY SHARE", change_id
        )
        response = await asyncio.wait_for(approve_item(item.id, _request(), pool=pool), 10)

    assert response.status_code == 200, _body(response)


async def test_approving_a_removal_does_not_wait_for_the_course_lock(term, pool, db_session):
    """A store holds the course's lock while it waits for items; deleting a
    change does not take that lock, as the Course changes delete does not."""
    change_id = await _applied_change(db_session)
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement), action="remove",
        change_id=change_id,
    )
    await db_session.commit()

    async with pool.acquire() as store, store.transaction():
        await store.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"course_changes|1151|{COURSE}",
        )
        response = await asyncio.wait_for(approve_item(item.id, _request(), pool=pool), 10)

    assert response.status_code == 200, _body(response)
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0


# --- Course changes carrying an item, and editing or deleting a sourced change ---


async def test_saving_a_change_with_its_add_item_approves_it(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(db_session, await _extraction(db_session, announcement))
    await db_session.commit()

    response = await create_change(
        "1151", _request(), pool=pool,
        body={"course_no": COURSE, "kind": "cancelled", "mode": "day", "day": THU,
              "item_id": item.id},
    )

    assert response.status_code == 200, _body(response)
    change = _body(response)
    assert change["day"] == THU
    assert await pool.fetchval(
        "SELECT source_announcement_id FROM course_changes WHERE id = $1", change["id"]
    ) == announcement.id
    assert tuple(await _item_row(pool, item.id))[:2] == ("approved", change["id"])
    assert (await _audit(pool))[0][1]["item_id"] == item.id


async def test_saving_with_an_item_of_another_course_writes_nothing(term, pool, db_session):
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement), course_no="EE2005701"
    )
    await db_session.commit()

    response = await create_change(
        "1151", _request(), pool=pool,
        body={"course_no": COURSE, "kind": "cancelled", "mode": "day", "day": TUE,
              "item_id": item.id},
    )
    bad_ids = [
        await create_change(
            "1151", _request(), pool=pool,
            body={"course_no": COURSE, "kind": "cancelled", "mode": "day", "day": TUE,
                  "item_id": item_id},
        )
        for item_id in ("7", 2**63)
    ]
    huge = [
        await approve_item(2**63, _request(), pool=pool),
        await reject_item(2**63, _request(), pool=pool, body={}),
    ]

    assert response.status_code == 409
    assert [r.status_code for r in bad_ids] == [400, 400]
    assert [r.status_code for r in huge] == [404, 404]
    assert await pool.fetchval("SELECT count(*) FROM course_changes") == 0
    assert (await _item_row(pool, item.id))["state"] == "pending"


async def test_saving_a_change_with_its_replace_item_approves_it(term, pool, db_session):
    change_id = await _applied_change(db_session, "room", room="IB-201")
    announcement = await _announcement(db_session)
    item = await _item(
        db_session, await _extraction(db_session, announcement),
        _change("room", room="IB-302"), action="replace", change_id=change_id,
    )
    await db_session.commit()

    response = await update_change(
        change_id, _request(), pool=pool,
        body={"kind": "room", "mode": "day", "day": TUE, "room": "IB-303", "item_id": item.id},
    )

    assert response.status_code == 200, _body(response)
    assert _body(response)["room"] == "IB-303"
    assert tuple(await _item_row(pool, item.id))[:2] == ("approved", change_id)


async def test_editing_a_sourced_change_marks_its_item_edited(term, pool, db_session):
    change_id = await _applied_change(db_session, "room", room="IB-201")
    announcement = await _announcement(db_session)
    extraction = await _extraction(db_session, announcement)
    applied = await _item(db_session, extraction, _change("room", room="IB-201"),
                          state="applied", change_id=change_id)
    rejected = await _item(db_session, extraction, _change("room", day=THU, room="X"),
                           state="rejected", change_id=change_id)
    await db_session.commit()

    response = await update_change(
        change_id, _request("lead@example.com"), pool=pool,
        body={"kind": "room", "mode": "day", "day": TUE, "room": "IB-305"},
    )

    assert response.status_code == 200, _body(response)
    assert tuple(await _item_row(pool, applied.id))[:3] == ("edited", change_id, "lead@example.com")
    assert (await _item_row(pool, rejected.id))["state"] == "rejected"


async def test_deleting_a_sourced_change_marks_its_item_and_supersedes_pending_ones(
    term, pool, db_session
):
    change_id = await _applied_change(db_session)
    announcement = await _announcement(db_session)
    applied = await _item(
        db_session, await _extraction(db_session, announcement, model="m1"), state="applied",
        change_id=change_id,
    )
    extraction = await _extraction(db_session, announcement)
    replace = await _item(
        db_session, extraction, _change(day=THU), action="replace", change_id=change_id
    )
    remove = await _item(db_session, extraction, action="remove", change_id=change_id)
    await db_session.commit()

    response = await delete_change(change_id, _request(), pool=pool)

    assert response.status_code == 200
    assert tuple(await _item_row(pool, applied.id)) == ("removed", None, "ops@example.com", None)
    for item in (replace, remove):
        assert tuple(await _item_row(pool, item.id)) == ("superseded", None, None, None)
    assert _body(await list_reviews("1151", _request(), pool=pool))["items"] == []
    assert _body(await pending_count(pool=pool)) == {"pending": 0}


# --- Reads -------------------------------------------------------------------


async def test_the_review_list_shows_pending_items_with_the_change_each_meets(
    term, pool, db_session
):
    room = await _applied_change(db_session, "room", room="IB-201")
    announcement = await _announcement(db_session)
    extraction = await _extraction(db_session, announcement)
    clashing = await _item(
        db_session, extraction, _change("room", room="IB-302"),
        checks={"rules": {"ok": False, "codes": ["clash"], "change_ids": [room]}},
    )
    replacing = await _item(db_session, extraction, _change("room", day=THU, room="IB-303"),
                            action="replace", change_id=room)
    unchecked = await _item(db_session, extraction, _change(day=THU))
    await _item(db_session, extraction, state="rejected")
    await db_session.commit()

    response = await list_reviews("1151", _request(path="/api/r"), pool=pool)

    body = _body(response)
    assert body["auto_apply"] is False
    existing = {i["id"]: i["existing_change_id"] for i in body["items"]}
    assert existing == {clashing.id: room, replacing.id: room, unchecked.id: None}
    assert body["items"][0]["subject"] == "11/17 停課"
    assert (await list_reviews("1150", _request(), pool=pool)).status_code == 404
    assert await _audit(pool) == [("user.list", {"route": "/api/r", "count": 3})]


async def test_the_announcement_list_searches_filters_and_pages(term, pool, db_session):
    for n in range(12):
        await _announcement(
            db_session, f"{200 + n}-{900 + n}", subject=f"第 {n} 週講義",
            posted=POSTED + timedelta(minutes=n), route="not_sent", codes=("MA1001703",),
        )
    failed = await _announcement(db_session, "300-1", subject="期中考", codes=("EE2005701",))
    await _extraction(db_session, failed, status="failed")
    changed = await _announcement(db_session, "301-2", subject="11/19 停課")
    await _extraction(db_session, changed, model="m1", raw={"course_changes": []})
    await _extraction(db_session, changed, model="m2", raw={"course_changes": [_change()]})
    await db_session.commit()

    async def page(**query):
        return _body(await list_announcements("1151", _request(), pool=pool, **query))

    first = await page()
    assert first["counts"] == {"all": 14, "extracted": 1, "failed": 1, "differs": 1}
    assert len(first["announcements"]) == 10
    assert len((await page(page=2))["announcements"]) == 4
    assert [a["id"] for a in (await page(extraction="differs"))["announcements"]] == ["301-2"]
    assert [a["id"] for a in (await page(extraction="failed"))["announcements"]] == ["300-1"]
    assert [a["id"] for a in (await page(q="ee2005"))["announcements"]] == ["300-1"]
    assert (await page(q="資料結構"))["counts"]["all"] == 1
    listed = (await page(q="11/19"))["announcements"][0]
    assert listed["excerpt"].startswith("同學好")
    assert listed["labels"] == [{"label": "class_cancel", "score": 0.97}]
    bad = [
        await list_announcements("1151", _request(), pool=pool, **query)
        for query in (
            {"extraction": "x"}, {"page": 0}, {"page": 2**62}, {"q": "停\x00課"},
            {"q": "停" * 201},
        )
    ]
    assert [r.status_code for r in bad] == [400] * 5
    assert (await page(q="停" * 200))["counts"]["all"] == 0


async def test_the_detail_shows_the_newest_extraction_and_skipped_codes(term, pool, db_session):
    announcement = await _announcement(db_session, codes=(COURSE, "CS2001302"))
    old = await _extraction(db_session, announcement, model="m1")
    await _item(db_session, old, state="superseded")
    done = await _extraction(db_session, announcement, model="m2")
    item = await _item(db_session, done)
    exam = await _item(
        db_session, done, {"kind": "midterm", "date": "2026-11-26"}, action=None,
        kind="exam", state="info", course_no=None,
    )
    await _item(db_session, done, {}, action=None, state="skipped", course_no="CS2001302")
    await _extraction(db_session, announcement, status="failed")
    await db_session.commit()

    body = _body(await get_announcement(announcement.id, _request(), pool=pool))

    assert body["model"] == "m2"
    assert body["extraction"]["status"] == "failed"
    assert [i["id"] for i in body["items"]] == [item.id, exam.id]
    assert body["skipped_codes"] == ["CS2001302"]
    assert body["moodle_url"] == "https://moodle.example/mod/forum/discuss.php?d=101"
    assert body["text"].startswith("11/17 停課")
    assert (await get_announcement("1-1", _request(), pool=pool)).status_code == 404
