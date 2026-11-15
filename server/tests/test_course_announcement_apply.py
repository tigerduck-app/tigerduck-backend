"""Shadow mode and automatic apply of extracted course changes. Every
announcement and answer here is invented."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select, update

from server.academic_calendar.models import CourseChange
from server.admin_audit import AdminAuditLog
from server.course_announcements.extractor import queue_extraction
from server.course_announcements.models import CourseAnnouncementItem
from server.tests.conftest import AnnouncementCourse, extracted_change, extraction_answer

pytestmark = pytest.mark.asyncio(loop_scope="session")

CANCEL_19 = extraction_answer(labels=["class_cancel"], course_changes=[extracted_change()])


def _cancel(day: str, evidence: str) -> dict:
    return extracted_change(start_date=day, end_date=day, evidence=evidence)


async def _audit(course: AnnouncementCourse) -> list[AdminAuditLog]:
    async with course.factory() as session:
        return list(await session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))


async def test_shadow_mode_marks_a_passing_change_and_writes_nothing(
    announcement_course: AnnouncementCourse,
):
    await announcement_course.post()

    await announcement_course.extract(CANCEL_19)

    (item,) = await announcement_course.items()
    assert (item.state, item.passes_checks, item.decided_by) == ("pending", True, None)
    assert await announcement_course.changes() == []
    assert await _audit(announcement_course) == []


async def test_automatic_apply_writes_the_change_with_its_source_actor_and_audit_row(
    announcement_course,
):
    key = await announcement_course.post()

    await announcement_course.extract(CANCEL_19, auto_apply=True)

    (item,) = await announcement_course.items()
    (change,) = await announcement_course.changes()
    assert (item.state, item.course_change_id, item.decided_by) == (
        "applied",
        change.id,
        "course-announcements",
    )
    assert item.decided_at == announcement_course.now
    assert (change.term_code, change.course_no, change.kind, change.mode) == (
        "1151",
        "CS1001301",
        "cancelled",
        "day",
    )
    assert (change.start_date, change.end_date, change.periods) == (
        date(2026, 11, 19),
        date(2026, 11, 19),
        None,
    )
    assert (change.created_by, change.updated_by, change.source_announcement_id) == (
        "course-announcements",
        "course-announcements",
        key,
    )
    (row,) = await _audit(announcement_course)
    assert (row.actor, row.action, row.target_user_id) == (
        "course-announcements",
        "course_change.create",
        None,
    )
    assert row.detail == {
        "change_id": change.id,
        "term": "1151",
        "course_no": "CS1001301",
        "kind": "cancelled",
        "announcement_id": key,
    }


async def test_one_failing_change_holds_every_change_of_the_announcement(announcement_course):
    await announcement_course.post(text="11/19（四）停課，11/18 也停課")

    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel"],
            # The course does not meet on Wednesday 11/18.
            course_changes=[extracted_change(), _cancel("2026-11-18", "11/18 也停課")],
        ),
        auto_apply=True,
    )

    items = await announcement_course.items()
    assert [(i.state, i.passes_checks) for i in items] == [("pending", True), ("pending", False)]
    assert await announcement_course.changes() == []
    assert await _audit(announcement_course) == []


async def test_changes_that_clash_with_each_other_are_held_together(announcement_course):
    await announcement_course.post(
        text="11/19 改到 IB-302；11/19 改到 IB-303", scores={"class_change": 0.95}
    )
    rooms = [
        extracted_change(kind="room", room=room, evidence=f"11/19 改到 {room}")
        for room in ("IB-302", "IB-303")
    ]

    await announcement_course.extract(
        extraction_answer(labels=["class_change"], course_changes=rooms), auto_apply=True
    )

    # Each passes alone, so the second clashes only once the first is written.
    items = await announcement_course.items()
    assert [(i.state, i.passes_checks) for i in items] == [("pending", True)] * 2
    assert await announcement_course.changes() == []
    assert await _audit(announcement_course) == []


async def test_a_corrected_date_waits_for_review_with_automatic_apply_on(announcement_course):
    await announcement_course.post()
    await announcement_course.extract(CANCEL_19, auto_apply=True)
    (applied,) = await announcement_course.changes()

    await announcement_course.post(text="更正：11/23（一）停課", edited=timedelta(hours=1))
    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel"], course_changes=[_cancel("2026-11-23", "11/23（一）停課")]
        ),
        auto_apply=True,
    )

    items = await announcement_course.items()
    assert [(i.action, i.state, i.payload["start_date"]) for i in items] == [
        ("add", "applied", "2026-11-19"),
        ("add", "pending", "2026-11-23"),
        ("remove", "pending", "2026-11-19"),
    ]
    assert items[1].passes_checks
    assert [c.id for c in await announcement_course.changes()] == [applied.id]


async def test_a_change_an_operator_deleted_is_not_applied_again(announcement_course):
    key = await announcement_course.post()
    await announcement_course.extract(CANCEL_19, auto_apply=True)
    # What deleting it in Course changes does.
    async with announcement_course.factory() as session:
        await session.execute(delete(CourseChange))
        await session.execute(update(CourseAnnouncementItem).values(state="removed"))
        assert await queue_extraction(session, key)
        await session.commit()
    announcement_course.now += timedelta(minutes=1)

    await announcement_course.extract(CANCEL_19, auto_apply=True)

    items = await announcement_course.items()
    assert [(i.state, i.passes_checks) for i in items] == [("removed", True), ("pending", True)]
    assert await announcement_course.changes() == []


async def test_a_rejected_change_is_not_applied_after_a_re_extract(announcement_course):
    key = await announcement_course.post()
    await announcement_course.extract(CANCEL_19)
    async with announcement_course.factory() as session:
        await session.execute(update(CourseAnnouncementItem).values(state="rejected"))
        assert await queue_extraction(session, key)
        await session.commit()
    announcement_course.now += timedelta(minutes=1)

    await announcement_course.extract(CANCEL_19, auto_apply=True)

    assert [i.state for i in await announcement_course.items()] == ["rejected", "pending"]
    assert await announcement_course.changes() == []


async def test_a_contradicted_change_is_not_applied(announcement_course):
    await announcement_course.post()
    await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    await announcement_course.extract(
        extraction_answer(
            labels=["class_change"],
            held_as_usual=[{"date": "2026-11-19", "evidence": "11/19 照常上課"}],
        ),
        auto_apply=True,
    )

    await announcement_course.extract(CANCEL_19, auto_apply=True)

    (item,) = await announcement_course.items()
    assert (item.state, item.checks["contradiction"]["ok"]) == ("pending", False)
    assert await announcement_course.changes() == []


async def test_a_replace_item_holds_the_announcements_add_items(announcement_course):
    key = await announcement_course.post(text="11/19 第 6、7 節改到 IB-302")
    await announcement_course.applied(
        key, date(2026, 11, 19), kind="room", room="IB-302", periods=["6", "7"]
    )
    text = "11/19 第 6、7 節改到 IB-303，11/23（一）停課"
    await announcement_course.post(text=text, edited=timedelta(hours=1))

    await announcement_course.extract(
        extraction_answer(
            labels=["class_change", "class_cancel"],
            course_changes=[
                extracted_change(
                    kind="room", room="IB-303", periods=["6", "7"],
                    evidence="11/19 第 6、7 節改到 IB-303",
                ),
                _cancel("2026-11-23", "11/23（一）停課"),
            ],
        ),
        auto_apply=True,
    )

    items = await announcement_course.items()
    assert [(i.action, i.state) for i in items] == [("replace", "pending"), ("add", "pending")]
    assert len(await announcement_course.changes()) == 1
