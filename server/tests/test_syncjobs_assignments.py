"""apply_fetched_assignments: insert / update / soft-delete / resurrect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from server.academic_calendar.models import SemesterTerm
from server.auth.models import User
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserChangeLog,
    UserCourse,
)
from server.syncjobs.assignments import (
    apply_fetched_assignments,
    listed_courses,
    moodle_semester,
)
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    FetchedCourse,
    HttpCourseFetcher,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime.now(UTC)


def _fa(assignment_id: int, **kwargs) -> FetchedAssignment:
    defaults = dict(
        moodle_course_id=7001,
        moodle_assignment_id=assignment_id,
        course_name="資料結構",
        title=f"HW{assignment_id}",
        due_at=NOW + timedelta(days=7),
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=(
            f"https://moodle.example.edu/mod/assign/view.php?id={assignment_id}"
        ),
        intro_html=None,
    )
    defaults.update(kwargs)
    return FetchedAssignment(**defaults)


async def _make_user(session) -> User:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    return user


async def _changelog(session, user_id):
    return (
        (
            await session.execute(
                select(UserChangeLog)
                .where(UserChangeLog.user_id == user_id)
                .order_by(UserChangeLog.revision)
            )
        )
        .scalars()
        .all()
    )


async def test_inserts_new_assignments_with_changelog(db_session):
    user = await _make_user(db_session)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    assert stats.fetched_count == 2
    assert stats.changed_count == 2
    rows = (
        (
            await db_session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.moodle_assignment_id for r in rows} == {1, 2}
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert all(e.entity_type == "assignment" for e in entries)
    assert all(e.operation == "upsert" for e in entries)
    assert all(e.device_id is None for e in entries)


async def test_unchanged_assignment_writes_no_changelog(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    later = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=later
    )
    await db_session.commit()

    assert stats.changed_count == 0
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 1
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.last_seen_at == later  # metadata still refreshed
    assert row.fetched_at == later


async def test_changed_field_updates_and_logs_field_names(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    new_due = NOW + timedelta(days=14)
    stats = await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1, due_at=new_due, title="HW1-renamed")],
        now=NOW + timedelta(hours=8),
    )
    await db_session.commit()

    assert stats.changed_count == 1
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert set(entries[-1].payload["fields"]) == {"due_at", "title"}
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.title == "HW1-renamed"
    assert row.due_at == new_due


async def test_absent_assignment_soft_deleted_then_resurrected(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    t2 = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=t2
    )
    await db_session.commit()
    assert stats.changed_count == 1
    gone = (
        await db_session.execute(
            select(UserAssignment).where(
                UserAssignment.user_id == user.id,
                UserAssignment.moodle_assignment_id == 2,
            )
        )
    ).scalar_one()
    assert gone.deleted_at is not None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "delete"
    assert entries[-1].entity_id == str(gone.id)

    t3 = NOW + timedelta(hours=16)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=t3
    )
    await db_session.commit()
    await db_session.refresh(gone)
    assert gone.deleted_at is None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "upsert"


def _course(course_id, idnumber, fullname="", start=None) -> FetchedCourse:
    return FetchedCourse(
        moodle_course_id=course_id,
        short_name="",
        full_name=fullname or f"{idnumber} Course",
        category_id=None,
        enrolled_user_count=None,
        idnumber=idnumber,
        start_at=start,
    )


@pytest.mark.parametrize(
    ("idnumber", "semester"),
    [
        ("1142EC1013701", "1142"),
        ("114hGD3115301", "114H"),
        ("11413CS3003302", "1141"),
        ("1142", None),
        ("", None),
        ("TRAINING-2026", None),
    ],
)
async def test_the_term_of_a_moodle_idnumber(idnumber, semester):
    assert moodle_semester(idnumber) == semester


async def test_the_app_lists_this_terms_courses_and_the_class_tables():
    """Moodle keeps past enrolments open. The app lists every course of the
    term and a class-table course whose Moodle course is another term's, not
    an older course or a course without a term."""
    enrolled = [
        _course(1, "1151CS1001301"),
        _course(2, "1151EE2005301"),
        _course(3, "1142PE1001301"),
        _course(4, "1142CS2006301"),
        _course(5, ""),
    ]
    assert listed_courses(enrolled, {"CS1001301", "PE1001301"}, "1151") == {
        1: "CS1001301",
        2: "EE2005301",
        3: "PE1001301",
    }


async def test_a_retaken_course_is_listed_in_its_latest_moodle_course():
    """One Moodle course per course number: this term's, else the one that
    began last."""
    failed = _course(10, "1132CS1001301", start=datetime(2025, 2, 1, tzinfo=UTC))
    older = _course(20, "1131EE2005301", start=datetime(2024, 9, 1, tzinfo=UTC))
    newer = _course(30, "1141EE2005301", start=datetime(2025, 9, 1, tzinfo=UTC))
    retaken = _course(40, "1151CS1001301", start=datetime(2026, 9, 1, tzinfo=UTC))
    listed = listed_courses(
        [failed, older, newer, retaken], {"CS1001301", "EE2005301"}, "1151"
    )
    assert listed == {30: "EE2005301", 40: "CS1001301"}


async def test_another_terms_course_is_listed_by_its_idnumbers_number_only():
    """The app matches the class table on the idnumber's number and reads a
    co-listed course's other numbers only to file what it lists."""
    co_listed = _course(
        7001,
        "1142AS5140701",
        fullname="114.2【半導體研究所】AS5140701 / 【資工系】CS5140701 晶片設計",
    )
    assert listed_courses([co_listed], {"CS5140701"}, "1151") == {}
    assert listed_courses([co_listed], {"AS5140701"}, "1151") == {7001: "AS5140701"}


async def test_a_co_listed_course_is_filed_under_the_class_tables_code():
    """A co-listed course's idnumber carries one department's code and its
    full name the other's; the class table holds the student's own."""
    co_listed = _course(
        19739,
        "1151AS5140701",
        fullname="115.1【半導體研究所】AS5140701 電腦輔助晶片系統設計"
        " / 【資工系】CS5140701 電腦輔助晶片系統設計",
    )
    assert listed_courses([co_listed], {"CS5140701"}, "1151") == {19739: "CS5140701"}
    assert listed_courses([co_listed], set(), "1151") == {19739: "AS5140701"}


async def test_the_course_fetch_reads_each_courses_idnumber_and_start():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["wsfunction"] == "core_webservice_get_site_info":
            return httpx.Response(200, json={"userid": 42})
        return httpx.Response(
            200,
            json=[
                {"id": 7001, "fullname": "DS", "idnumber": "1151CS2006301",
                 "startdate": 1788220800},
                {"id": 7002, "fullname": "Training", "idnumber": "", "startdate": 0},
                "not a course",
                {"id": 7003, "fullname": "Lab", "idnumber": "1151CS2006303",
                 "startdate": 10**20},
            ],
        )

    fetcher = HttpCourseFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )

    courses = await fetcher.fetch_courses(token="tok")

    assert [(c.idnumber, c.start_at) for c in courses] == [
        ("1151CS2006301", datetime(2026, 9, 1, 0, 0, tzinfo=UTC)),
        ("", None),
        ("1151CS2006303", None),
    ]


async def _publish_term(session, code="1151"):
    today = NOW.date()
    session.add(
        SemesterTerm(
            code=code,
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=90),
        )
    )
    await session.flush()


async def _class_table_course(session, user, course_no, semester="1151"):
    course = UserCourse(
        user_id=user.id,
        semester=semester,
        course_key=f"client:{semester}:{course_no}",
        course_no=course_no,
        course_name="資料結構",
        moodle_id=f"{semester}{course_no}",
    )
    session.add(course)
    await session.flush()
    return course


async def _rows(session, user):
    return {
        r.moodle_assignment_id: r
        for r in await session.scalars(
            select(UserAssignment)
            .where(UserAssignment.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    }


async def test_links_an_assignment_to_the_class_table_course_the_app_files_it_under(
    db_session,
):
    """The apps send a course's term and number; Moodle's assignment list
    names the course by its numeric id, which only the enrolment list ties to
    the idnumber, the term and the number."""
    user = await _make_user(db_session)
    await _publish_term(db_session)
    course = await _class_table_course(db_session, user, "CS2006301")
    await _class_table_course(db_session, user, "CS2006301", semester="1142")
    enrolled = [
        _course(7001, "1151CS2006301"),
        _course(7002, "1151GE1001301"),
        _course(7003, "1141MA1001301"),
        _course(7004, ""),
    ]

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(i, moodle_course_id=7000 + i) for i in (1, 2, 3, 4, 5)],
        now=NOW,
        enrolled=enrolled,
    )
    await db_session.commit()

    rows = await _rows(db_session, user)
    # A course without a term, or missing from the course list, is of none.
    assert [
        (rows[i].user_course_id, rows[i].course_no, rows[i].moodle_semester)
        for i in (1, 2, 3, 4, 5)
    ] == [
        (course.id, "CS2006301", "1151"),
        (None, "GE1001301", "1151"),
        (None, None, "1141"),
        (None, None, ""),
        (None, None, ""),
    ]


async def test_a_dropped_class_table_course_takes_no_assignment(db_session):
    user = await _make_user(db_session)
    await _publish_term(db_session)
    course = await _class_table_course(db_session, user, "CS2006301")
    course.enrollment_status = "dropped"

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1)],
        now=NOW,
        enrolled=[_course(7001, "1151CS2006301")],
    )
    await db_session.commit()

    assert (await _rows(db_session, user))[1].user_course_id is None


@pytest.mark.parametrize("enrolled", [None, []], ids=["failed", "empty"])
async def test_without_the_course_list_a_fetch_leaves_the_links_alone(
    db_session, enrolled
):
    """A course list that failed, or that names no course while Moodle lists
    assignments, cannot classify the rows."""
    user = await _make_user(db_session)
    await _publish_term(db_session)
    course = await _class_table_course(db_session, user, "CS2006301")
    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1)],
        now=NOW,
        enrolled=[_course(7001, "1151CS2006301")],
    )
    await db_session.commit()

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1), _fa(2)],
        now=NOW,
        enrolled=enrolled,
    )
    await db_session.commit()

    rows = await _rows(db_session, user)
    assert [
        (rows[i].user_course_id, rows[i].course_no, rows[i].moodle_semester)
        for i in (1, 2)
    ] == [(course.id, "CS2006301", "1151"), (None, None, None)]


async def test_a_course_no_longer_listed_loses_its_link(db_session):
    user = await _make_user(db_session)
    await _publish_term(db_session)
    course = await _class_table_course(db_session, user, "CS1001301")
    old = _course(7001, "1142CS1001301", start=datetime(2026, 2, 1, tzinfo=UTC))
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW, enrolled=[old]
    )
    await db_session.commit()
    assert (await _rows(db_session, user))[1].user_course_id == course.id

    # This term's Moodle course of the number opens; the app drops the old one.
    retaken = _course(7101, "1151CS1001301", start=datetime(2026, 9, 1, tzinfo=UTC))
    stats = await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1), _fa(2, moodle_course_id=7101)],
        now=NOW,
        enrolled=[old, retaken],
    )
    await db_session.commit()

    rows = await _rows(db_session, user)
    assert (rows[1].user_course_id, rows[2].user_course_id) == (None, course.id)
    assert stats.changed_count == 2
    assert (await _changelog(db_session, user.id))[-2].payload == {
        "fields": ["user_course_id"]
    }


async def test_with_no_term_published_nothing_is_linked(db_session):
    user = await _make_user(db_session)
    await _class_table_course(db_session, user, "CS2006301")

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1)],
        now=NOW,
        enrolled=[_course(7001, "1151CS2006301")],
    )
    await db_session.commit()

    row = (await _rows(db_session, user))[1]
    assert (row.user_course_id, row.moodle_semester) == (None, "1151")


async def test_never_touches_provider_submission_fields(db_session):
    user = await _make_user(db_session)
    existing = UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=1,
        title="HW1",
        provider_is_submitted=True,
        provider_submitted_at=NOW - timedelta(days=1),
    )
    db_session.add(existing)
    await db_session.commit()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, title="HW1")], now=NOW
    )
    await db_session.commit()
    await db_session.refresh(existing)
    assert existing.provider_is_submitted is True
    assert existing.provider_submitted_at is not None


@pytest.mark.parametrize(
    ("refused_course_ids", "deleted"),
    [
        (frozenset({7001}), {"left_out"}),
        (frozenset(), {"refused", "left_out", "placeholder"}),
    ],
    ids=["a-course-refused", "none-refused"],
)
async def test_an_answer_that_lost_every_course_keeps_what_it_refused(
    db_session, refused_course_ids, deleted
):
    """It says nothing about the courses it refused, nor about a
    placeholder, which names no course and may be of one of them. The rows
    of a course it left out, such as one the user is a TA in, go as after
    any answer."""
    user = User(student_id="b00000001")
    db_session.add(user)
    await db_session.flush()
    rows = {
        "refused": UserAssignment(
            user_id=user.id, moodle_course_id=7001, moodle_assignment_id=1, title="HW1"
        ),
        "left_out": UserAssignment(
            user_id=user.id, moodle_course_id=7003, moodle_assignment_id=2, title="HW2"
        ),
        "placeholder": UserAssignment(
            user_id=user.id,
            moodle_course_id=0,
            moodle_assignment_id=3,
            course_no="CS0000001",
            title="HW3",
        ),
    }
    db_session.add_all(rows.values())
    await db_session.commit()

    stats = await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[],
        now=NOW,
        refused_course_ids=refused_course_ids,
    )
    await db_session.commit()

    for row in rows.values():
        await db_session.refresh(row)
    assert {name for name, row in rows.items() if row.deleted_at is not None} == deleted
    assert stats.changed_count == len(deleted)
    entries = await _changelog(db_session, user.id)
    assert sorted((e.operation, e.entity_id) for e in entries) == sorted(
        ("delete", str(rows[name].id)) for name in deleted
    )


@pytest.mark.parametrize("deleted", [False, True], ids=["live", "soft-deleted"])
async def test_a_fetch_takes_over_the_apps_placeholder(db_session, deleted):
    """An app uploads an assignment before the server's fetch lists it, as a
    placeholder with course id 0. The fetch makes that row its own, so the
    mark and the submission it holds stay on the assignment's only row
    instead of on a second one the fetch soft-deletes."""
    user = await _make_user(db_session)
    placeholder = UserAssignment(
        user_id=user.id,
        moodle_course_id=0,
        moodle_assignment_id=1,
        course_no="CS2006301",
        title="HW1 as the app saw it",
        provider_is_submitted=True,
        deleted_at=NOW - timedelta(hours=8) if deleted else None,
    )
    db_session.add(placeholder)
    await db_session.flush()
    db_session.add(
        UserAssignmentOverride(
            user_id=user.id, user_assignment_id=placeholder.id, local_status="ignored"
        )
    )
    await db_session.commit()

    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(
                UserAssignment.id,
                UserAssignment.moodle_course_id,
                UserAssignment.title,
                UserAssignment.deleted_at,
                UserAssignment.provider_is_submitted,
            )
            .where(UserAssignment.user_id == user.id)
            .order_by(UserAssignment.id)
        )
    ).all()
    assert [tuple(row) for row in rows] == [(placeholder.id, 7001, "HW1", None, True)]
    marks = (
        await db_session.execute(
            select(
                UserAssignmentOverride.user_assignment_id,
                UserAssignmentOverride.local_status,
            )
        )
    ).all()
    assert [tuple(mark) for mark in marks] == [(placeholder.id, "ignored")]
    assert stats.changed_count == 1
    entries = await _changelog(db_session, user.id)
    assert [(e.operation, e.entity_id) for e in entries] == [
        ("upsert", str(placeholder.id))
    ]
    assert "moodle_course_id" in entries[0].payload["fields"]


async def test_a_fetch_listing_an_assignment_under_two_courses_keeps_both(db_session):
    """Moodle gives an assignment one course, but a fetch that names it
    under two still applies: the first course takes the app's placeholder
    over, with its mark, and the second gets a row of its own."""
    user = await _make_user(db_session)
    placeholder = UserAssignment(
        user_id=user.id, moodle_course_id=0, moodle_assignment_id=1, title="HW1"
    )
    db_session.add(placeholder)
    await db_session.flush()
    db_session.add(
        UserAssignmentOverride(
            user_id=user.id, user_assignment_id=placeholder.id, local_status="ignored"
        )
    )
    await db_session.commit()

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1), _fa(1, moodle_course_id=7002)],
        now=NOW,
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(
                UserAssignment.id,
                UserAssignment.moodle_course_id,
                UserAssignment.deleted_at,
            )
            .where(UserAssignment.user_id == user.id)
            .order_by(UserAssignment.moodle_course_id)
        )
    ).all()
    assert [(row.moodle_course_id, row.deleted_at) for row in rows] == [
        (7001, None),
        (7002, None),
    ]
    assert rows[0].id == placeholder.id
    marks = (
        await db_session.execute(select(UserAssignmentOverride.user_assignment_id))
    ).scalars()
    assert list(marks) == [placeholder.id]


async def test_a_known_extension_stays_in_the_dates_the_list_writes(db_session):
    """Moodle's list carries no extension, so a sync keeps the one the probe
    stored: the due date stays the extension, and an extension past the
    cutoff moves the cutoff too."""
    user = await _make_user(db_session)
    due, cutoff = NOW + timedelta(days=1), NOW + timedelta(days=2)
    extension = NOW + timedelta(days=3)
    listed = _fa(1, due_at=due, cutoff_at=cutoff)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[listed], now=NOW
    )
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    row.due_at = row.cutoff_at = row.extension_due_at = extension
    await db_session.commit()
    logged = len(await _changelog(db_session, user.id))

    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[listed], now=NOW
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert (row.due_at, row.cutoff_at, row.extension_due_at) == (
        extension,
        extension,
        extension,
    )
    assert stats.changed_count == 0
    assert stats.recheck_ids == frozenset()
    assert len(await _changelog(db_session, user.id)) == logged


async def test_taking_over_a_placeholder_keeps_the_apps_extended_date(db_session):
    """The app uploads the date that applies to the student. A later one
    than Moodle's is their extension, kept until the probe answers, so the
    date does not go back to Moodle's when the fetch takes the row over."""
    user = await _make_user(db_session)
    due = NOW + timedelta(days=1)
    extended = NOW + timedelta(days=3)
    placeholder = UserAssignment(
        user_id=user.id,
        moodle_course_id=0,
        moodle_assignment_id=1,
        title="HW1",
        due_at=extended,
    )
    db_session.add(placeholder)
    await db_session.commit()

    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1, due_at=due, cutoff_at=due + timedelta(days=1))],
        now=NOW,
    )
    await db_session.commit()

    await db_session.refresh(placeholder)
    assert (placeholder.due_at, placeholder.cutoff_at) == (extended, extended)
    assert placeholder.extension_due_at == extended
    entries = await _changelog(db_session, user.id)
    assert "due_at" not in entries[0].payload["fields"]


async def test_a_placeholder_with_moodles_date_or_earlier_has_no_extension(db_session):
    user = await _make_user(db_session)
    due = NOW + timedelta(days=1)
    placeholder = UserAssignment(
        user_id=user.id,
        moodle_course_id=0,
        moodle_assignment_id=1,
        title="HW1",
        due_at=due - timedelta(hours=1),
    )
    db_session.add(placeholder)
    await db_session.commit()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, due_at=due)], now=NOW
    )
    await db_session.commit()

    await db_session.refresh(placeholder)
    assert (placeholder.due_at, placeholder.extension_due_at) == (due, None)


@pytest.mark.parametrize("moodle_moves", [-48, 48], ids=["earlier", "later"])
async def test_a_due_date_moved_back_is_handed_to_the_probe(db_session, moodle_moves):
    """An app's initial upload stores the student's date on a row with a
    real course id, and a teacher can move a due date earlier: a row whose
    stored date the list moves back is probed, whose answer may move it
    forward again. Moodle's date stands meanwhile."""
    user = await _make_user(db_session)
    stored = NOW + timedelta(days=7)
    row = UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=1,
        title="HW1",
        due_at=stored,
    )
    db_session.add(row)
    await db_session.commit()
    listed = stored + timedelta(hours=moodle_moves)

    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, due_at=listed)], now=NOW
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert row.due_at == listed
    assert stats.recheck_ids == (frozenset({1}) if moodle_moves < 0 else frozenset())


@pytest.mark.parametrize("moved_by", [0, 24], ids=["to-the-extension", "past-it"])
async def test_an_extension_moodles_due_date_reaches_is_dropped(db_session, moved_by):
    """A teacher moves the due date to the student's extension or past it:
    the date no longer comes from the extension, so it is not kept, and a
    withdrawal of it later cannot cancel a reminder (`push/presend.py`)."""
    user = await _make_user(db_session)
    extension = NOW + timedelta(days=3)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, due_at=NOW + timedelta(days=1))], now=NOW
    )
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    row.due_at = row.extension_due_at = extension
    await db_session.commit()
    listed = extension + timedelta(hours=moved_by)

    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, due_at=listed)], now=NOW
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert (row.due_at, row.extension_due_at) == (listed, None)
    assert stats.recheck_ids == frozenset()
