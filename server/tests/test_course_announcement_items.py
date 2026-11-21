"""The review items an extraction turns into: add, replace and remove per
course code, skipped codes, make-up classes and exams shown as they are,
contradictions across announcements and items already past. Every
announcement and answer here is invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.course_announcements.history import queue_reextraction
from server.course_announcements.ingest import queue_extractions
from server.course_announcements.items import mark_past_items
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementExtraction,
)
from server.scheduler.runtime import build_scheduler
from server.tests.conftest import (
    AnnouncementCourse,
    extracted_change,
    extracted_exam,
    extraction_answer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CANCEL_19 = extraction_answer(labels=["class_cancel"], course_changes=[extracted_change()])
HELD_19 = extraction_answer(
    labels=["class_change"], held_as_usual=[{"date": "2026-11-19", "evidence": "11/19 照常上課"}]
)
NOT_SENT = {"class_cancel": 0.01}
EXAM_INFO = {"exam_info": 0.98}
# On a Tuesday, when the course has no class.
FINAL = extracted_exam(kind="final", date="2027-01-05", evidence="期末考 1/5")


def _summary(items) -> list[tuple]:
    return [
        (item.course_no, item.kind, item.action, item.state, item.payload.get("start_date"))
        for item in items
    ]


async def test_a_change_is_an_add_item_per_code_with_class_times_and_the_rest_skipped(
    announcement_course: AnnouncementCourse,
):
    await announcement_course.post(codes=("CS1001301", "CS1001302"))

    await announcement_course.extract(CANCEL_19)

    items = await announcement_course.items()
    assert _summary(items) == [
        ("CS1001301", "course_change", "add", "pending", "2026-11-19"),
        ("CS1001302", "course_change", None, "skipped", None),
    ]
    add = items[0]
    assert add.passes_checks and add.evidence == "11/19（四）停課"
    assert add.payload == {
        "kind": "cancelled",
        "mode": "day",
        "start_date": "2026-11-19",
        "end_date": "2026-11-19",
        "excluded_weekdays": [],
        "periods": None,
        "room": None,
        "start_time": None,
        "end_time": None,
        "note_zh": None,
        "note_en": None,
    }
    assert add.checks["contradiction"] == {"ok": True, "announcement_id": None}
    assert await announcement_course.changes() == []


async def test_a_change_stated_twice_is_one_item(announcement_course):
    await announcement_course.post()

    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel"],
            course_changes=[extracted_change(), extracted_change(evidence="11/19（四）停課")],
        )
    )

    assert len(await announcement_course.items()) == 1


async def test_make_up_classes_and_exams_are_shown_and_wait_for_nobody(announcement_course):
    await announcement_course.post(text="11/21 補課；期末考 1/5，範圍第一到四章；小考另行通知")
    makeup = {
        "date": "2026-11-21",
        "start_time": "09:10",
        "end_time": None,
        "periods": None,
        "room": "IB-302",
        "evidence": "11/21 補課",
    }
    final = extracted_exam(
        kind="final", date="2027-01-05", scope="第一到四章", evidence="期末考 1/5，範圍第一到四章"
    )
    quiz = {**final, "kind": "quiz", "date": None, "scope": None, "evidence": "小考另行通知"}

    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], makeup_classes=[makeup], exams=[final, quiz])
    )

    items = await announcement_course.items()
    assert [(i.course_no, i.kind, i.action, i.state) for i in items] == [
        (None, "makeup", None, "info"),
        (None, "exam", None, "info"),
        (None, "exam", None, "info"),
    ]
    assert items[0].payload == {k: v for k, v in makeup.items() if k != "evidence"}
    assert [i.evidence for i in items] == ["11/21 補課", "期末考 1/5，範圍第一到四章", "小考另行通知"]
    assert await announcement_course.changes() == []


async def test_a_midterm_announced_for_a_class_day_is_a_pending_assessment(
    announcement_course,
):
    await announcement_course.post(
        text="11/16（一）期中考，範圍第一到四章",
        scores=EXAM_INFO,
        codes=("CS1001301", "CS1001302"),
    )

    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam(scope="第一到四章")])
    )

    item, exam = await announcement_course.items()
    assert (item.course_no, item.kind, item.action, item.state) == (
        "CS1001301", "course_change", "add", "pending",
    )
    assert (exam.kind, exam.state, exam.payload["scope"]) == ("exam", "info", "第一到四章")
    assert item.payload == {
        "kind": "assessment",
        "mode": "day",
        "start_date": "2026-11-16",
        "end_date": "2026-11-16",
        "excluded_weekdays": [],
        "periods": None,
        "room": None,
        "start_time": None,
        "end_time": None,
        "note_zh": "期中考",
        "note_en": "Midterm exam",
    }
    assert item.evidence == "11/16（一）期中考"
    assert item.checks["exam_details"] == {"ok": True}
    assert item.passes_checks


@pytest.mark.parametrize(
    "details",
    [{"room": "TR-313"}, {"start_time": "13:00"}, {"end_time": "15:00"}],
    ids=["room", "start", "end"],
)
async def test_an_exam_in_its_own_room_or_time_waits_for_review(announcement_course, details):
    await announcement_course.post(text="11/16（一）期中考", scores=EXAM_INFO)

    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam(**details)]),
        auto_apply=True,
    )

    item, exam = await announcement_course.items()
    assert (item.payload["kind"], item.state, item.passes_checks) == ("assessment", "pending", False)
    assert item.checks["exam_details"] == {"ok": False}
    assert {field: exam.payload[field] for field in details} == details
    assert await announcement_course.changes() == []


async def test_an_exam_whose_room_and_start_are_extracted_changes_applies(announcement_course):
    text = "11/16（一）期中考改至 TR-313，13:00 開始"
    await announcement_course.post(text=text, scores={"exam_info": 0.98, "class_change": 0.98})

    await announcement_course.extract(
        extraction_answer(
            labels=["exam_info", "class_change"],
            exams=[extracted_exam(room=" TR-313", start_time="13:00", evidence=text)],
            course_changes=[
                extracted_change(kind=kind, start_date="2026-11-16", end_date="2026-11-16",
                                 evidence=text, **field)
                for kind, field in (("room", {"room": "TR-313"}), ("start", {"start_time": "13:00"}))
            ],
        ),
        auto_apply=True,
    )

    assert sorted(c.kind for c in await announcement_course.changes()) == [
        "assessment", "room", "start",
    ]


@pytest.mark.parametrize(
    ("kind", "contradicted"), [("quiz", True), ("midterm", False)], ids=["another", "the-same"]
)
async def test_a_later_post_stating_another_assessment_contradicts_an_older_one(
    announcement_course, kind, contradicted
):
    await announcement_course.post(text="11/16（一）期中考", scores=EXAM_INFO)
    later = await announcement_course.post(
        2, text="11/16（一）期中考", posted=timedelta(days=1), scores=EXAM_INFO
    )
    # The later post goes to the LLM first, in shadow mode.
    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam(kind=kind)])
    )
    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam()]), auto_apply=True
    )

    item, _ = await announcement_course.items()
    assert item.checks["contradiction"] == {
        "ok": not contradicted, "announcement_id": later if contradicted else None,
    }
    assert [c.kind for c in await announcement_course.changes()] == (
        [] if contradicted else ["assessment"]
    )


async def test_a_later_post_stating_another_assessment_marks_a_pending_one(announcement_course):
    await announcement_course.post(text="11/16（一）期中考", scores=EXAM_INFO)
    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam()])
    )

    later = await announcement_course.post(
        2, text="11/16（一）小考", posted=timedelta(days=1), scores=EXAM_INFO
    )
    await announcement_course.extract(
        extraction_answer(
            labels=["exam_info"], exams=[extracted_exam(kind="quiz", evidence="11/16（一）小考")]
        )
    )

    item, _ = await announcement_course.items()
    assert (item.state, item.passes_checks) == ("pending", False)
    assert item.checks["contradiction"] == {"ok": False, "announcement_id": later}


@pytest.mark.parametrize(
    ("kind", "names"), [("final", ("期末考", "Final exam")), ("quiz", ("小考", "Quiz"))]
)
async def test_an_exam_is_named_by_its_kind(announcement_course, kind, names):
    await announcement_course.post(text="11/16（一）考試", scores=EXAM_INFO)

    await announcement_course.extract(
        extraction_answer(
            labels=["exam_info"],
            exams=[extracted_exam(kind=kind, name="考試", evidence="11/16（一）考試")],
        )
    )

    item, _ = await announcement_course.items()
    assert (item.payload["note_zh"], item.payload["note_en"]) == names


async def test_an_exam_of_another_kind_keeps_its_name_and_waits_for_an_english_one(
    announcement_course,
):
    await announcement_course.post(text="11/16（一）期中專題展示", scores=EXAM_INFO)

    await announcement_course.extract(
        extraction_answer(
            labels=["exam_info"],
            exams=[
                extracted_exam(kind="other", name=" 期中專題展示 ", evidence="11/16（一）期中專題展示")
            ],
        ),
        auto_apply=True,
    )

    item, _ = await announcement_course.items()
    assert (item.state, item.payload["note_zh"], item.payload["note_en"]) == (
        "pending", "期中專題展示", None,
    )
    assert not item.passes_checks
    assert item.checks["rules"]["codes"] == ["format"]
    assert await announcement_course.changes() == []


@pytest.mark.parametrize("day", ["2026-11-17", "2027-01-11", None])
async def test_an_exam_on_a_day_without_class_stays_information(announcement_course, day):
    """A Tuesday the course does not meet, a Monday after the term, and no date."""
    await announcement_course.post(text="期中考", scores=EXAM_INFO)

    await announcement_course.extract(
        extraction_answer(labels=["exam_info"], exams=[extracted_exam(date=day, evidence="期中考")])
    )

    assert [(i.kind, i.state, i.payload["date"]) for i in await announcement_course.items()] == [
        ("exam", "info", day),
    ]


async def test_a_class_moved_online_is_an_online_item(announcement_course):
    await announcement_course.post(text="11/19（四）改線上上課", scores={"class_change": 0.98})

    await announcement_course.extract(
        extraction_answer(
            labels=["class_change"],
            course_changes=[extracted_change(kind="online", evidence="11/19（四）改線上上課")],
        )
    )

    (item,) = await announcement_course.items()
    assert (item.kind, item.action, item.state, item.payload["kind"]) == (
        "course_change", "add", "pending", "online",
    )
    assert (item.payload["room"], item.payload["note_zh"]) == (None, None)
    assert item.passes_checks


async def test_an_edit_changing_an_applied_room_is_a_replace_item(announcement_course):
    key = await announcement_course.post(text="11/19 第 6、7 節改到 IB-302")
    applied = await announcement_course.applied(
        key, date(2026, 11, 19), kind="room", room="IB-302", periods=["6", "7"]
    )
    room = extracted_change(
        kind="room", room="IB-303", periods=["6", "7"], evidence="11/19 第 6、7 節改到 IB-303"
    )

    await announcement_course.post(text="11/19 第 6、7 節改到 IB-303", edited=timedelta(hours=1))
    await announcement_course.extract(
        extraction_answer(labels=["class_change"], course_changes=[room])
    )

    (item,) = await announcement_course.items()
    assert (item.action, item.state, item.course_change_id) == ("replace", "pending", applied)
    assert item.payload["room"] == "IB-303"
    assert item.checks["rules"]["ok"]


def _room(room: str, period: str) -> dict:
    return extracted_change(
        kind="room", room=room, periods=[period], evidence=f"11/19 第 {period} 節改到 {room}"
    )


async def _two_rooms(announcement_course) -> tuple[str, int, int]:
    """Announcement 1 with its two applied room changes on Thursday 11/19,
    period 6 and period 7."""
    key = await announcement_course.post(text="11/19 第 6 節改到 IB-302，第 7 節改到 IB-303")
    six = await announcement_course.applied(
        key, date(2026, 11, 19), kind="room", room="IB-302", periods=["6"]
    )
    seven = await announcement_course.applied(
        key, date(2026, 11, 19), kind="room", room="IB-303", periods=["7"]
    )
    return key, six, seven


async def test_an_edit_replaces_the_change_of_its_periods(announcement_course):
    _, _, seven = await _two_rooms(announcement_course)

    await announcement_course.post(
        text="11/19 第 6 節改到 IB-302，第 7 節改到 T4-201", edited=timedelta(hours=1)
    )
    await announcement_course.extract(
        extraction_answer(
            labels=["class_change"],
            course_changes=[_room("IB-302", "6"), _room("T4-201", "7")],
        )
    )

    (item,) = await announcement_course.items()
    assert (item.action, item.course_change_id, item.payload["room"]) == (
        "replace",
        seven,
        "T4-201",
    )


async def test_an_edit_dropping_one_periods_change_removes_that_one(announcement_course):
    _, _, seven = await _two_rooms(announcement_course)

    await announcement_course.post(text="11/19 第 6 節改到 IB-302", edited=timedelta(hours=1))
    await announcement_course.extract(
        extraction_answer(labels=["class_change"], course_changes=[_room("IB-302", "6")])
    )

    (item,) = await announcement_course.items()
    assert (item.action, item.course_change_id) == ("remove", seven)


async def test_two_facts_never_replace_one_applied_change(announcement_course):
    key = await announcement_course.post(text="11/19 第 6、7 節改到 IB-302")
    applied = await announcement_course.applied(
        key, date(2026, 11, 19), kind="room", room="IB-302", periods=["6", "7"]
    )

    await announcement_course.post(
        text="11/19 第 6 節改到 T4-201，第 7 節改到 T4-202", edited=timedelta(hours=1)
    )
    await announcement_course.extract(
        extraction_answer(
            labels=["class_change"],
            course_changes=[_room("T4-201", "6"), _room("T4-202", "7")],
        )
    )

    items = await announcement_course.items()
    assert [(i.action, i.course_change_id) for i in items] == [
        ("replace", applied),
        ("add", None),
    ]


async def test_a_held_day_on_a_weekday_an_applied_range_leaves_out_removes_nothing(
    announcement_course,
):
    first = await announcement_course.post(text="11/16 到 11/30 週一停課")
    await announcement_course.applied(
        first,
        date(2026, 11, 16),
        mode="range",
        end_date=date(2026, 11, 30),
        excluded_weekdays=[2, 3, 4, 5, 6, 7],
    )
    later = await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )

    await announcement_course.extract(HELD_19)

    assert await announcement_course.items(later) == []


async def test_an_unchanged_applied_change_needs_no_item(announcement_course):
    key = await announcement_course.post()
    await announcement_course.applied(key, date(2026, 11, 19))

    await announcement_course.extract(CANCEL_19)

    assert await announcement_course.items() == []


async def _extractions(course: AnnouncementCourse, key: str) -> list[str]:
    async with course.factory() as session:
        rows = await session.scalars(
            select(CourseAnnouncementExtraction.status)
            .where(CourseAnnouncementExtraction.announcement_id == key)
            .order_by(CourseAnnouncementExtraction.id)
        )
        return list(rows)


async def test_an_edit_routed_not_sent_still_removes_the_change_taken_from_it(
    announcement_course,
):
    key = await announcement_course.post()
    applied = await announcement_course.applied(key, date(2026, 11, 19))

    await announcement_course.post(
        text="更正：本週照常上課", edited=timedelta(hours=1), scores=NOT_SENT
    )
    assert await _extractions(announcement_course, key) == ["superseded", "queued"]
    await announcement_course.extract(extraction_answer())

    items = await announcement_course.items()
    assert _summary(items) == [
        ("CS1001301", "course_change", "remove", "pending", "2026-11-19"),
    ]
    assert items[0].course_change_id == applied


async def test_an_edit_routed_not_sent_with_no_change_taken_from_it_queues_nothing(
    announcement_course,
):
    key = await announcement_course.post()
    await announcement_course.extract(CANCEL_19)

    await announcement_course.post(
        text="更正：本週照常上課", edited=timedelta(hours=1), scores=NOT_SENT
    )

    assert await _extractions(announcement_course, key) == ["done"]


async def test_reextract_queues_a_not_sent_announcement_a_change_was_taken_from(
    announcement_course,
):
    key = await announcement_course.post(scores=NOT_SENT)
    other = await announcement_course.post(2, scores=NOT_SENT)
    await announcement_course.applied(key, date(2026, 11, 19))

    assert await queue_reextraction(announcement_course.factory, announcement_course.now) == 1

    assert await _extractions(announcement_course, key) == ["queued"]
    assert await _extractions(announcement_course, other) == []


async def test_a_corrected_date_removes_the_applied_day_and_adds_the_new_one(
    announcement_course,
):
    key = await announcement_course.post()
    applied = await announcement_course.applied(key, date(2026, 11, 19))

    await announcement_course.post(text="更正：11/23（一）停課", edited=timedelta(hours=1))
    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel"],
            course_changes=[
                extracted_change(
                    start_date="2026-11-23", end_date="2026-11-23", evidence="11/23（一）停課"
                )
            ],
        )
    )

    items = await announcement_course.items()
    assert _summary(items) == [
        ("CS1001301", "course_change", "add", "pending", "2026-11-23"),
        ("CS1001301", "course_change", "remove", "pending", "2026-11-19"),
    ]
    assert (items[1].course_change_id, items[1].evidence) == (applied, None)
    assert [c.id for c in await announcement_course.changes()] == [applied]


async def test_a_later_notice_held_as_usual_removes_an_applied_cancellation(
    announcement_course,
):
    first = await announcement_course.post()
    applied = await announcement_course.applied(first, date(2026, 11, 19))

    later = await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    await announcement_course.extract(HELD_19)

    (item,) = await announcement_course.items(later)
    assert (item.action, item.state, item.course_change_id) == ("remove", "pending", applied)
    assert item.evidence == "11/19 照常上課"
    assert [c.id for c in await announcement_course.changes()] == [applied]


async def test_a_later_notice_held_as_usual_contradicts_a_pending_cancellation(
    announcement_course,
):
    await announcement_course.post()
    await announcement_course.extract(CANCEL_19)

    later = await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    await announcement_course.extract(HELD_19)

    (item,) = await announcement_course.items()
    assert item.state == "pending" and not item.passes_checks
    assert item.checks["contradiction"] == {"ok": False, "announcement_id": later}
    assert await announcement_course.items(later) == []


async def test_a_group_only_later_post_contradicts_and_leaves_the_decision(
    announcement_course,
):
    await announcement_course.post()
    await announcement_course.extract(CANCEL_19)

    later = await announcement_course.post(
        2,
        text="11/19 照常上課",
        posted=timedelta(days=1),
        scores={"class_change": 0.95},
        group_id=12,
    )
    await announcement_course.extract(HELD_19)

    (item,) = await announcement_course.items()
    assert (item.state, item.checks["contradiction"]["announcement_id"]) == ("pending", later)


async def test_an_older_announcement_extracted_after_a_later_held_notice_is_contradicted(
    announcement_course,
):
    await announcement_course.post()
    later = await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    # The later post goes to the LLM first.
    await announcement_course.extract(HELD_19)
    await announcement_course.extract(CANCEL_19)

    (item,) = await announcement_course.items()
    assert (item.action, item.state, item.passes_checks) == ("add", "pending", False)
    assert item.checks["contradiction"] == {"ok": False, "announcement_id": later}


async def test_an_applied_change_extracted_again_after_a_later_held_notice_is_removed(
    announcement_course,
):
    first = await announcement_course.post()
    await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    await announcement_course.extract(HELD_19)
    applied = await announcement_course.applied(first, date(2026, 11, 19))

    await announcement_course.extract(CANCEL_19)

    (item,) = await announcement_course.items(first)
    assert (item.action, item.state, item.course_change_id) == ("remove", "pending", applied)


async def test_storing_a_new_version_of_the_later_post_lifts_the_contradiction(
    announcement_course,
):
    await announcement_course.post()
    await announcement_course.extract(CANCEL_19)
    await announcement_course.post(
        2, text="11/19 照常上課", posted=timedelta(days=1), scores={"class_change": 0.95}
    )
    await announcement_course.extract(HELD_19)

    await announcement_course.post(
        2,
        text="投影片已上傳",
        posted=timedelta(days=1),
        edited=timedelta(hours=1),
        scores={"materials": 0.97},
    )

    (item,) = await announcement_course.items()
    assert item.checks["contradiction"] == {"ok": True, "announcement_id": None}
    assert item.passes_checks


async def test_facts_whose_days_have_passed_are_past_at_extraction(announcement_course):
    announcement_course.now = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)
    await announcement_course.post(text="11/19（四）停課，11/18 已補課")

    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel"],
            course_changes=[extracted_change()],
            makeup_classes=[
                {
                    "date": "2026-11-18",
                    "start_time": None,
                    "end_time": None,
                    "periods": None,
                    "room": None,
                    "evidence": "11/18 已補課",
                }
            ],
        )
    )

    assert [(i.kind, i.state) for i in await announcement_course.items()] == [
        ("course_change", "past"),
        ("makeup", "past"),
    ]


async def test_the_daily_pass_marks_waiting_items_past_the_next_day_in_taipei(
    announcement_course,
):
    await announcement_course.post(text="11/19（四）停課，11/20 小考；期末考另行公告")
    # On a Friday, when the course has no class.
    exam = extracted_exam(kind="quiz", date="2026-11-20", evidence="11/20 小考")
    undated = {**exam, "kind": "final", "date": None, "evidence": "期末考另行公告"}
    await announcement_course.extract(
        extraction_answer(
            labels=["class_cancel", "exam_info"],
            course_changes=[extracted_change()],
            exams=[exam, undated],
        )
    )
    factory = announcement_course.factory

    # 23:59 on 11/20 in Taipei, then midnight.
    assert await mark_past_items(factory, datetime(2026, 11, 20, 15, 59, tzinfo=UTC)) == 1
    assert await mark_past_items(factory, datetime(2026, 11, 20, 16, 0, tzinfo=UTC)) == 1

    assert [(i.kind, i.state) for i in await announcement_course.items()] == [
        ("course_change", "past"),
        ("exam", "past"),
        ("exam", "info"),
    ]


async def test_a_re_extract_leaves_one_current_set_of_exams(announcement_course):
    key = await announcement_course.post(text="期末考 1/5")
    answer = extraction_answer(labels=["exam_info"], exams=[FINAL])
    await announcement_course.extract(answer)
    async with announcement_course.factory() as session:
        assert await queue_extractions(session, "1151", CourseAnnouncement.id == key) == 1
        await session.commit()
    announcement_course.now += timedelta(minutes=1)

    await announcement_course.extract(answer)

    assert [(i.kind, i.state) for i in await announcement_course.items()] == [
        ("exam", "superseded"),
        ("exam", "info"),
    ]


async def test_an_edited_post_leaves_one_current_set_of_exams(announcement_course):
    await announcement_course.post(text="期末考 1/5")
    await announcement_course.extract(extraction_answer(labels=["exam_info"], exams=[FINAL]))

    await announcement_course.post(text="更正：期末考 1/6", edited=timedelta(hours=1))
    assert [i.state for i in await announcement_course.items()] == ["superseded"]
    await announcement_course.extract(
        extraction_answer(
            labels=["exam_info"],
            exams=[{**FINAL, "date": "2027-01-06", "evidence": "更正：期末考 1/6"}],
        )
    )

    items = await announcement_course.items()
    assert [(i.state, i.payload["date"]) for i in items] == [
        ("superseded", "2027-01-05"),
        ("info", "2027-01-06"),
    ]


async def test_a_store_shares_the_term_and_changes_it_reads_under_the_course_lock(
    announcement_course, prepared_engine
):
    """A key share lock, since the portal updates a change FOR NO KEY UPDATE
    before it takes the course lock."""
    await announcement_course.post()
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        seen.append(statement)

    event.listen(prepared_engine.sync_engine, "before_cursor_execute", record)
    try:
        await announcement_course.extract(CANCEL_19)
    finally:
        event.remove(prepared_engine.sync_engine, "before_cursor_execute", record)

    locks = {
        table: [s.split(" FOR ", 1)[1] for s in seen if f"FROM {table}" in s and " FOR " in s]
        for table in ("semester_terms", "course_changes")
    }
    assert locks == {
        "semester_terms": ["KEY SHARE"],
        "course_changes": ["KEY SHARE OF course_changes"],
    }


async def test_the_daily_pass_is_scheduled_after_midnight_in_taipei(test_settings):
    settings = test_settings.model_copy(
        update={"announcement_classifier_url": "http://classifier.test:40002"}
    )
    scheduler = build_scheduler(async_sessionmaker(), object(), settings, llm=object())

    job = scheduler.get_job("course_announcement_past")
    fields = {f.name: str(f) for f in job.trigger.fields if not f.is_default}
    assert fields == {"hour": "0", "minute": "5"}
    assert str(job.trigger.timezone) == "Asia/Taipei"
    assert build_scheduler(
        async_sessionmaker(), object(), test_settings, llm=object()
    ).get_job("course_announcement_past") is None
