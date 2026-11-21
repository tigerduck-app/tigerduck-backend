"""The checks an extracted course change passes before it may apply, one by
one. Every announcement and answer here is invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from server.academic_calendar.models import AcademicHoliday, CourseChange, SemesterTerm
from server.auth.models import User
from server.course_announcements.checks import (
    Announcement,
    Statement,
    change_payload,
    check_change,
    contradiction,
    exam_payload,
    has_schedule,
    lock_courses,
    passes,
    stated_weekdays,
)
from server.sync.models import UserCourse
from server.tests.conftest import extracted_change, extracted_exam

pytestmark = pytest.mark.asyncio(loop_scope="session")

POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
# Monday periods 3-4 and Thursday 6-7.
MEETS = {"1": ["3", "4"], "4": ["6", "7"]}
TEXT = "停課通知\n\n11/19（四）停課，11/19 第 6、7 節改到 IB-302。"


def _announcement(**fields) -> Announcement:
    return Announcement(
        **{
            "text": TEXT,
            "labels": ["class_cancel", "class_change"],
            "group_id": -1,
            "posted_at": POSTED,
            "visible_from": None,
            "llm_labels": ["class_cancel", "class_change"],
            "certain": True,
            **fields,
        }
    )


@pytest_asyncio.fixture(loop_scope="session")
async def course(db_session):
    """Term 1151 with a holiday on Thursday 11/26, and a student of
    CS1001301 with class times."""
    db_session.add_all(
        [
            SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8)),
            AcademicHoliday(
                name_zh="校慶", name_en="Anniversary",
                start_date=date(2026, 11, 26), end_date=date(2026, 11, 26),
            ),
        ]
    )
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserCourse(
            user_id=user.id, semester="1151", course_key="client:1151:CS1001301",
            course_no="CS1001301", course_name="資料結構", schedule_json=MEETS,
        )
    )
    await db_session.commit()


async def _check(
    db_session, fact=None, *, announcement=None, course_no="CS1001301", payload=None, **kw
):
    term_days = await lock_courses(db_session, "1151", [course_no])
    checks = await check_change(
        db_session,
        term="1151",
        term_days=term_days,
        course_no=course_no,
        payload=payload or change_payload(fact or extracted_change()),
        evidence=(fact or extracted_change())["evidence"],
        announcement=announcement or _announcement(),
        **kw,
    )
    await db_session.rollback()
    return checks


def _failed(checks) -> list[str]:
    return [name for name, check in checks.items() if not check["ok"]]


async def test_a_cancellation_on_a_class_day_passes_every_check(db_session, course):
    checks = await _check(db_session)

    assert _failed(checks) == []
    assert passes(checks)
    assert checks["rules"] == {"ok": True, "codes": [], "change_ids": []}


async def test_evidence_must_be_quoted_from_the_text(db_session, course):
    for evidence in ("11/19 停課", "", "   "):
        checks = await _check(db_session, extracted_change(evidence=evidence))
        assert _failed(checks) == ["evidence"]


async def test_a_day_outside_the_term_fails_the_term_check(db_session, course):
    fact = extracted_change(start_date="2027-01-14", end_date="2027-01-14", evidence="停課")

    assert "term" in _failed(await _check(db_session, fact))


async def test_a_term_not_published_fails_the_term_check(db_session, course):
    term_days = await lock_courses(db_session, "1152", ["CS1001301"])

    assert term_days is None


async def test_a_day_the_course_does_not_meet_fails_the_class_day_check(db_session, course):
    fact = extracted_change(start_date="2026-11-18", end_date="2026-11-18", evidence="停課")

    assert _failed(await _check(db_session, fact)) == ["class_day"]


async def test_a_holiday_fails_the_class_day_check(db_session, course):
    fact = extracted_change(start_date="2026-11-26", end_date="2026-11-26", evidence="停課")

    assert _failed(await _check(db_session, fact)) == ["class_day"]


@pytest.mark.parametrize(
    ("evidence", "ok"),
    [
        ("11/19（四）停課", True),
        ("11/19 週四停課", True),
        ("下週四停課", True),
        ("11/19 停課", True),
        ("11/19（三）停課", False),
        ("11/19 星期三停課", False),
        ("No class on Wed., 11/19", False),
        ("No class on Thursday 11/19", True),
    ],
)
async def test_a_stated_weekday_must_match_the_date(db_session, course, evidence, ok):
    text = f"停課通知\n\n{evidence}"
    checks = await _check(
        db_session, extracted_change(evidence=evidence), announcement=_announcement(text=text)
    )

    assert checks["weekday"]["ok"] is ok
    assert _failed(checks) == ([] if ok else ["weekday"])


async def test_stated_weekdays_read_each_form():
    assert stated_weekdays("週一至週五、星期六、禮拜天（三）(二) Fri, sunday") == {
        1, 2, 3, 5, 6, 7,
    }
    assert stated_weekdays("第三週，週末，Sunny Monterey") == set()


async def test_a_period_the_course_does_not_meet_in_breaks_the_rules(db_session, course):
    fact = extracted_change(periods=["3"])

    checks = await _check(db_session, fact)

    assert _failed(checks) == ["rules"]
    assert checks["rules"]["codes"] == ["missing_periods"]


async def test_a_value_its_kind_needs_missing_breaks_the_rules(db_session, course):
    fact = extracted_change(kind="room", room=None, evidence="11/19 第 6、7 節改到 IB-302")

    checks = await _check(db_session, fact)

    assert checks["rules"] == {"ok": False, "codes": ["format"], "change_ids": []}


async def test_a_day_change_spanning_days_breaks_the_rules(db_session, course):
    fact = extracted_change(end_date="2026-11-23")

    assert (await _check(db_session, fact))["rules"]["codes"] == ["format"]


async def test_a_clash_with_an_operators_range_change_names_it_and_leaves_it(
    db_session, course
):
    range_change = CourseChange(
        term_code="1151", course_no="CS1001301", kind="room", mode="range",
        start_date=date(2026, 11, 1), end_date=date(2026, 11, 30), excluded_weekdays=[],
        room="T4-201", created_by="ops@example.com", updated_by="ops@example.com",
    )
    db_session.add(range_change)
    await db_session.commit()
    change_id = range_change.id
    fact = extracted_change(
        kind="room", room="IB-302", periods=["6", "7"], evidence="11/19 第 6、7 節改到 IB-302"
    )

    checks = await _check(db_session, fact)

    assert _failed(checks) == ["rules"]
    assert checks["rules"] == {"ok": False, "codes": ["clash"], "change_ids": [change_id]}
    stored = await db_session.scalar(
        select(CourseChange).execution_options(populate_existing=True)
    )
    assert (stored.room, stored.start_date, stored.end_date) == (
        "T4-201",
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    # Replacing that change itself is no clash.
    assert (await _check(db_session, fact, editing=change_id))["rules"]["ok"]


@pytest.mark.parametrize(
    ("kind", "classifier", "llm", "ok"),
    [
        ("cancelled", ["class_cancel"], ["class_cancel"], True),
        ("cancelled", ["class_change"], ["class_cancel"], False),
        ("cancelled", ["class_cancel"], [], False),
        ("room", ["class_change"], ["class_change"], True),
        ("room", ["class_cancel"], ["class_change"], False),
    ],
)
async def test_the_classifier_and_the_llm_must_agree_on_the_kind(
    db_session, course, kind, classifier, llm, ok
):
    fact = extracted_change(
        kind=kind,
        room="IB-302" if kind == "room" else None,
        evidence="11/19 第 6、7 節改到 IB-302" if kind == "room" else "11/19（四）停課",
    )

    checks = await _check(
        db_session, fact, announcement=_announcement(labels=classifier, llm_labels=llm)
    )

    assert checks["labels"]["ok"] is ok


@pytest.mark.parametrize(
    ("classifier", "llm", "ok"),
    [
        (["exam_info"], ["exam_info"], True),
        (["exam_change"], ["exam_info", "class_change"], True),
        (["class_change"], ["exam_info"], False),
        (["exam_info"], ["class_change"], False),
    ],
)
async def test_an_assessment_takes_an_exam_label_from_both(
    db_session, course, classifier, llm, ok
):
    exam = extracted_exam(date="2026-11-19", evidence="11/19（四）停課")

    checks = await _check(
        db_session, exam, payload=exam_payload(exam),
        announcement=_announcement(labels=classifier, llm_labels=llm),
    )

    assert checks["labels"]["ok"] is ok
    assert checks["rules"]["ok"]


async def test_an_assessment_without_its_english_name_breaks_the_rules(db_session, course):
    exam = extracted_exam(kind="other", name="期中專題", date="2026-11-19")

    payload = exam_payload(exam)
    checks = await _check(db_session, exam, payload=payload)

    assert (payload["note_zh"], payload["note_en"]) == ("期中專題", None)
    assert checks["rules"] == {"ok": False, "codes": ["format"], "change_ids": []}


async def test_an_online_class_needs_no_description(db_session, course):
    fact = extracted_change(kind="online", note_zh="  ")

    assert (await _check(db_session, extracted_change(kind="online")))["rules"]["ok"]
    assert (await _check(db_session, fact))["rules"]["codes"] == ["format"]


async def test_an_uncertain_extraction_fails_the_certainty_check(db_session, course):
    checks = await _check(db_session, announcement=_announcement(certain=False))

    assert _failed(checks) == ["certain"]


@pytest.mark.parametrize(
    "fields",
    [{"group_id": 12}, {"visible_from": POSTED + timedelta(days=1)}],
    ids=["group", "visible-later"],
)
async def test_a_post_not_seen_by_the_whole_course_from_its_post_fails(
    db_session, course, fields
):
    checks = await _check(db_session, announcement=_announcement(**fields))

    assert _failed(checks) == ["visibility"]


async def test_a_post_visible_from_its_post_time_passes(db_session, course):
    checks = await _check(db_session, announcement=_announcement(visible_from=POSTED))

    assert checks["visibility"]["ok"]


async def test_a_course_without_class_times_has_no_schedule(db_session, course):
    assert await has_schedule(db_session, "1151", "CS1001301")
    assert not await has_schedule(db_session, "1151", "CS1001302")


def _statement(n: int, posted: datetime, *, held=(), changes=(), codes=("CS1001301",)):
    return Statement(
        announcement_id=f"{n}-{n}",
        posted_at=posted,
        course_codes=frozenset(codes),
        changes=tuple(change_payload(c) for c in changes),
        held=tuple(held),
    )


async def test_a_later_post_holding_the_day_as_usual_contradicts_the_change():
    statements = [
        _statement(1, POSTED - timedelta(days=1), held=[date(2026, 11, 19)]),
        _statement(2, POSTED + timedelta(days=1), held=[date(2026, 11, 19)]),
        _statement(3, POSTED + timedelta(days=2), held=[date(2026, 11, 19)]),
        _statement(4, POSTED + timedelta(days=3), held=[date(2026, 11, 19)], codes=["EE1"]),
    ]

    mark = contradiction(
        course_no="CS1001301",
        payload=change_payload(extracted_change()),
        announcement_id="0-0",
        posted_at=POSTED,
        statements=statements,
    )

    assert mark == {"ok": False, "announcement_id": "3-3"}


async def test_a_change_for_other_periods_of_the_day_does_not_contradict():
    room = extracted_change(kind="room", room="IB-302", periods=["3", "4"])
    later = _statement(
        1,
        POSTED + timedelta(hours=1),
        changes=[extracted_change(kind="room", room="T4-201", periods=["6", "7"])],
    )

    mark = contradiction(
        course_no="CS1001301",
        payload=change_payload(room),
        announcement_id="0-0",
        posted_at=POSTED,
        statements=[later],
    )

    assert mark == {"ok": True, "announcement_id": None}


async def test_a_day_held_as_usual_on_a_weekday_the_range_leaves_out_does_not_contradict():
    weekdays = extracted_change(
        mode="range", start_date="2026-11-16", end_date="2026-11-30", excluded_weekdays=[4]
    )

    def mark(held: date):
        return contradiction(
            course_no="CS1001301",
            payload=change_payload(weekdays),
            announcement_id="0-0",
            posted_at=POSTED,
            statements=[_statement(1, POSTED + timedelta(hours=1), held=[held])],
        )

    # Thursday 11/19 is left out; Monday 11/23 is not.
    assert mark(date(2026, 11, 19))["ok"]
    assert not mark(date(2026, 11, 23))["ok"]


async def test_only_a_different_change_of_the_kind_contradicts():
    room = extracted_change(kind="room", room="IB-302")
    later = POSTED + timedelta(hours=1)

    def mark(*changes):
        return contradiction(
            course_no="CS1001301",
            payload=change_payload(room),
            announcement_id="0-0",
            posted_at=POSTED,
            statements=[_statement(1, later, changes=changes)],
        )

    assert mark(room)["ok"]
    assert mark(extracted_change())["ok"]
    assert mark(extracted_change(kind="room", room="IB-302", start_date="2026-11-23",
                                 end_date="2026-11-23"))["ok"]
    assert mark(extracted_change(kind="room", room="T4-201")) == {
        "ok": False,
        "announcement_id": "1-1",
    }


async def test_a_later_online_class_contradicts_a_room_change_and_the_reverse():
    room = extracted_change(kind="room", room="IB-302")
    online = extracted_change(kind="online", room=None)
    later = POSTED + timedelta(hours=1)

    def mark(payload, stated):
        return contradiction(
            course_no="CS1001301",
            payload=change_payload(payload),
            announcement_id="0-0",
            posted_at=POSTED,
            statements=[_statement(1, later, changes=[stated])],
        )

    assert mark(room, online) == {"ok": False, "announcement_id": "1-1"}
    assert mark(online, room) == {"ok": False, "announcement_id": "1-1"}
    assert mark(online, online)["ok"]
