"""The review items the portal's Course changes routes approve and record,
against the tables the backend migrates. In the server suite for the reason
test_portal_sync_job_retry.py gives. Every announcement here is invented."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.course_changes import (  # noqa: E402
    create_change,
    delete_change,
    update_change,
)
from server.academic_calendar.models import CourseChange, SemesterTerm  # noqa: E402
from server.auth.models import User  # noqa: E402
from server.course_announcements.models import (  # noqa: E402
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
)
from server.sync.models import UserCourse  # noqa: E402

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
    state: str = "pending", kind: str = "course_change", course_no: str = COURSE,
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
    bad_id = await create_change(
        "1151", _request(), pool=pool,
        body={"course_no": COURSE, "kind": "cancelled", "mode": "day", "day": TUE,
              "item_id": "7"},
    )

    assert (response.status_code, bad_id.status_code) == (409, 400)
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


async def test_deleting_a_sourced_change_marks_its_item_removed(term, pool, db_session):
    change_id = await _applied_change(db_session)
    announcement = await _announcement(db_session)
    extraction = await _extraction(db_session, announcement)
    applied = await _item(db_session, extraction, state="applied", change_id=change_id)
    pending = await _item(db_session, extraction, action="replace", change_id=change_id)
    await db_session.commit()

    response = await delete_change(change_id, _request(), pool=pool)

    assert response.status_code == 200
    assert tuple(await _item_row(pool, applied.id))[:2] == ("removed", None)
    # A pending replace loses the change it would replace, and can then only be rejected.
    assert tuple(await _item_row(pool, pending.id))[:2] == ("pending", None)
