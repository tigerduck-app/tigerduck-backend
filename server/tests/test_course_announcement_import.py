"""The history import and its re-extract run, from an invented export.
Every announcement here is invented."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from server.academic_calendar.models import SemesterTerm
from server.course_announcements.history import (
    ExportRefused,
    import_history,
    queue_reextraction,
    read_export,
)
from server.course_announcements.models import CourseAnnouncement, CourseAnnouncementExtraction
from server.db import build_session_factory
from server.tests.conftest import RecordingClassifier

pytestmark = pytest.mark.asyncio(loop_scope="session")

POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
LAST_YEAR = datetime(2025, 10, 1, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def _post(n: int, subject: str, *, posted: datetime = POSTED, **fields) -> dict:
    return {
        "id": 900 + n,
        "discussion": 100 + n,
        "subject": subject,
        "message": f"<p>Invented notice {n}</p>",
        "created": int(posted.timestamp()),
        "modified": int(posted.timestamp()),
        "groupid": -1,
        "timestart": 0,
        "attachment": False,
        **fields,
    }


def _line(course_id: int, idnumber: str, fullname: str, *posts: dict) -> str:
    course = {"id": course_id, "idnumber": idnumber, "fullname": fullname}
    return json.dumps({"course": course, "discussions": list(posts)})


CURRENT = _line(
    7001,
    "1151CS2006301",
    "Data Structures CS2006301 / EE2006301",
    _post(1, "Class cancelled"),
    _post(2, "Slides"),
)
EARLIER = _line(
    6001, "1141CS2006301", "Data Structures", _post(3, "Class cancelled", posted=LAST_YEAR)
)
NO_TERM = _line(5001, "", "Invented club", _post(4, "Class cancelled"))
EXPORT = [CURRENT, "\n", EARLIER, NO_TERM]


@pytest.fixture
def factory(prepared_engine):
    return build_session_factory(prepared_engine)


@pytest.fixture
def classifier() -> RecordingClassifier:
    return RecordingClassifier(
        {"Class cancelled": {"class_cancel": 0.98}, "Slides": {"materials": 0.97}}
    )


@pytest_asyncio.fixture(loop_scope="session")
async def term(db_session):
    """1151 is the current term."""
    db_session.add(
        SemesterTerm(code="1151", start_date=date(2026, 9, 7), end_date=date(2027, 1, 15))
    )
    await db_session.commit()


async def _extractions(session) -> list[tuple]:
    rows = await session.execute(
        select(
            CourseAnnouncementExtraction.announcement_id,
            CourseAnnouncementExtraction.version,
            CourseAnnouncementExtraction.status,
        )
        .order_by(CourseAnnouncementExtraction.id)
        .execution_options(populate_existing=True)
    )
    return [tuple(row) for row in rows.all()]


async def _finish_extractions(session) -> None:
    await session.execute(update(CourseAnnouncementExtraction).values(status="done"))
    await session.commit()


async def test_a_first_run_stores_and_classifies_all_and_queues_only_the_current_term(
    db_session, factory, classifier, term
):
    result = await import_history(factory, classifier, read_export(EXPORT))

    assert (result.stored, result.classified, result.skipped_courses) == (3, 3, 1)
    rows = await db_session.execute(
        select(
            CourseAnnouncement.id,
            CourseAnnouncement.term_code,
            CourseAnnouncement.course_codes,
            CourseAnnouncement.route,
            CourseAnnouncement.raw_message,
        ).order_by(CourseAnnouncement.id)
    )
    assert [tuple(row) for row in rows] == [
        ("101-901", "1151", ["CS2006301", "EE2006301"], "sent", None),
        ("102-902", "1151", ["CS2006301", "EE2006301"], "not_sent", None),
        ("103-903", "1141", ["CS2006301"], "sent", None),
    ]
    assert await _extractions(db_session) == [("101-901", POSTED, "queued")]


async def test_a_second_run_stores_and_queues_nothing(db_session, factory, classifier, term):
    await import_history(factory, classifier, read_export(EXPORT))
    await _finish_extractions(db_session)
    sent = len(classifier.posts)

    result = await import_history(factory, classifier, read_export(EXPORT))

    assert (result.stored, result.classified) == (0, 0)
    assert len(classifier.posts) == sent
    assert await _extractions(db_session) == [("101-901", POSTED, "done")]


@pytest.mark.parametrize("field", ["userfullname", "usermodifiedfullname", "userid"])
async def test_an_author_field_stops_the_run_before_anything_is_stored(
    db_session, factory, classifier, term, field
):
    export = [
        CURRENT,
        _line(6001, "1141CS2006301", "Data Structures", _post(3, "Exam", **{field: "Invented"})),
    ]

    with pytest.raises(ExportRefused) as refused:
        await import_history(factory, classifier, read_export(export))

    assert str(refused.value) == f"line 2: field '{field}' is not in the export format"
    assert (await db_session.scalars(select(CourseAnnouncement.id))).all() == []
    assert classifier.posts == []


@pytest.mark.parametrize(
    "line",
    ["{not json", json.dumps({"course": {"id": 1}, "discussions": [{"id": 1}]})],
    ids=["not-json", "missing-values"],
)
async def test_a_malformed_line_stops_the_run(line):
    with pytest.raises(ExportRefused, match="^line 2: "):
        read_export([CURRENT, line])


async def test_reextract_queues_the_current_terms_sent_announcements_again(
    db_session, factory, classifier, term
):
    export = [
        CURRENT,
        EARLIER,
        _line(7002, "1151EE1001301", "Circuits", _post(5, "Class cancelled")),
    ]
    await import_history(factory, classifier, read_export(export))
    await _finish_extractions(db_session)
    await import_history(
        factory,
        classifier,
        read_export([_line(7003, "1151EE1002301", "Signals", _post(6, "Class cancelled"))]),
    )

    assert await queue_reextraction(factory, NOW) == 2

    assert sorted(await _extractions(db_session)) == [
        ("101-901", POSTED, "done"),
        ("101-901", POSTED, "queued"),
        ("105-905", POSTED, "done"),
        ("105-905", POSTED, "queued"),
        ("106-906", POSTED, "queued"),
    ]
    assert await queue_reextraction(factory, NOW) == 0
