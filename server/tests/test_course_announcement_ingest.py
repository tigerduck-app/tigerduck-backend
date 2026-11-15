"""Storing, classifying and routing course announcements. Every
announcement here is invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import func, select, update

from server.academic_calendar.models import SemesterTerm
from server.course_announcements.ingest import (
    classify_pending,
    route_for,
    store_announcements,
)
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
)
from server.db import build_session_factory
from server.tests.conftest import RecordingClassifier
from server.syncjobs.moodle_client import FetchedDiscussion

pytestmark = pytest.mark.asyncio(loop_scope="session")

POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
CANCELLED = {"class_cancel": 0.98}
MATERIALS = {"materials": 0.97}


def _discussion(
    n: int = 1, *, subject: str = "Class cancelled", edited: timedelta = timedelta(0), **fields
) -> FetchedDiscussion:
    return FetchedDiscussion(
        **{
            "discussion_id": 100 + n,
            "post_id": 900 + n,
            "subject": subject,
            "message": f"<p>Invented notice {n}: {subject}</p>",
            "created_at": POSTED,
            "modified_at": POSTED + edited,
            "group_id": -1,
            "visible_from": None,
            "has_attachment": False,
            "pinned": False,
            "starred": False,
            **fields,
        }
    )


@pytest_asyncio.fixture(loop_scope="session")
async def term(db_session):
    """1151 is the current term."""
    db_session.add(
        SemesterTerm(code="1151", start_date=date(2026, 9, 7), end_date=date(2027, 1, 15))
    )
    await db_session.commit()


async def _store(
    session, *discussions, course_codes=("CS1001301",), term_code="1151"
) -> list[str]:
    stored = await store_announcements(
        session,
        term_code=term_code,
        moodle_course_id=7001,
        course_codes=list(course_codes),
        discussions=list(discussions),
    )
    await session.commit()
    return stored


async def _announcement(session, key: str = "101-901") -> CourseAnnouncement:
    return await session.get(CourseAnnouncement, key, populate_existing=True)


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


@pytest.fixture
def factory(prepared_engine):
    return build_session_factory(prepared_engine)


async def test_a_new_announcement_is_stored_unclassified_with_its_raw_text(db_session):
    assert await _store(db_session, _discussion(group_id=12, has_attachment=True)) == ["101-901"]

    row = await _announcement(db_session)
    assert (row.route, row.subject, row.text, row.scores) == (
        "unclassified",
        "Class cancelled",
        None,
        None,
    )
    assert row.raw_subject == "Class cancelled"
    assert row.raw_message == "<p>Invented notice 1: Class cancelled</p>"
    assert (row.posted_at, row.modified_at, row.group_id, row.has_attachment) == (
        POSTED,
        POSTED,
        12,
        True,
    )
    columns = set(CourseAnnouncement.__table__.columns.keys())
    assert not {c for c in columns if "user" in c or "author" in c}


async def test_classifying_keeps_the_text_drops_the_raw_post_and_queues_a_sent_one(
    db_session, factory, term
):
    await _store(db_session, _discussion())
    classifier = RecordingClassifier({"Class cancelled": CANCELLED}, release="model-2026-11-15")

    assert await classify_pending(factory, classifier) == 1

    row = await _announcement(db_session)
    assert (row.route, row.raw_subject, row.raw_message) == ("sent", None, None)
    assert row.text == "Class cancelled\n\n<p>Invented notice 1: Class cancelled</p>"
    assert row.labels == ["class_cancel"]
    assert row.scores["class_cancel"] == 0.98 and len(row.scores) == 20
    assert row.classifier_release == "model-2026-11-15"
    assert await _extractions(db_session) == [("101-901", POSTED, "queued")]
    assert await classify_pending(factory, classifier) == 0


async def test_an_earlier_terms_announcement_is_classified_but_never_queued(
    db_session, factory, term
):
    await _store(db_session, _discussion(), term_code="1141")
    classifier = RecordingClassifier({"Class cancelled": CANCELLED})

    assert await classify_pending(factory, classifier) == 1

    assert (await _announcement(db_session)).route == "sent"
    assert await _extractions(db_session) == []


async def test_a_not_sent_announcement_queues_nothing(db_session, factory):
    await _store(db_session, _discussion(subject="Slides"))

    await classify_pending(factory, RecordingClassifier({"Slides": MATERIALS}))

    assert (await _announcement(db_session)).route == "not_sent"
    assert await _extractions(db_session) == []


async def test_an_edit_replaces_the_text_scores_and_extraction(db_session, factory, term):
    classifier = RecordingClassifier(
        {"Class cancelled": CANCELLED, "Class cancelled, moved": CANCELLED}
    )
    await _store(db_session, _discussion())
    await classify_pending(factory, classifier)

    edit = _discussion(subject="Class cancelled, moved", edited=timedelta(minutes=12))
    assert await _store(db_session, edit) == ["101-901"]

    row = await _announcement(db_session)
    assert (row.route, row.subject, row.text, row.scores, row.labels) == (
        "unclassified",
        "Class cancelled, moved",
        None,
        None,
        None,
    )
    assert row.modified_at == POSTED + timedelta(minutes=12)
    assert await _extractions(db_session) == [("101-901", POSTED, "superseded")]

    await classify_pending(factory, classifier)

    assert (await _announcement(db_session)).text.startswith("Class cancelled, moved")
    assert await _extractions(db_session) == [
        ("101-901", POSTED, "superseded"),
        ("101-901", POSTED + timedelta(minutes=12), "queued"),
    ]


async def test_an_edit_sent_nowhere_still_supersedes_the_pending_items(
    db_session, factory, term
):
    """The new version no longer reads as a class change, so nothing would
    replace the old version's items: storing it retires them at once."""
    await _store(db_session, _discussion())
    await classify_pending(factory, RecordingClassifier({"Class cancelled": CANCELLED}))
    extraction = (await db_session.scalars(select(CourseAnnouncementExtraction))).one()
    extraction.status = "done"
    db_session.add_all(
        [
            CourseAnnouncementItem(
                extraction_id=extraction.id,
                course_no="CS1001301",
                kind="course_change",
                action="add",
                payload={"kind": "cancelled", "start_date": "2026-11-19"},
                state=state,
            )
            for state in ("pending", "rejected")
        ]
    )
    await db_session.commit()

    await _store(db_session, _discussion(subject="Slides", edited=timedelta(hours=1)))
    await classify_pending(factory, RecordingClassifier({"Slides": MATERIALS}))

    assert (await _announcement(db_session)).route == "not_sent"
    states = await db_session.scalars(
        select(CourseAnnouncementItem.state)
        .order_by(CourseAnnouncementItem.id)
        .execution_options(populate_existing=True)
    )
    assert states.all() == ["superseded", "rejected"]
    assert await _extractions(db_session) == [("101-901", POSTED, "done")]


@pytest.mark.parametrize(
    "late", [timedelta(minutes=-5), timedelta(0)], ids=["older", "unchanged"]
)
async def test_an_older_or_unchanged_copy_changes_nothing(db_session, factory, term, late):
    edited = timedelta(minutes=30)
    await _store(db_session, _discussion(edited=edited))
    await classify_pending(factory, RecordingClassifier({"Class cancelled": CANCELLED}))

    copy = _discussion(subject="Other words", edited=edited + late)
    assert await _store(db_session, copy) == []

    row = await _announcement(db_session)
    assert (row.route, row.subject) == ("sent", "Class cancelled")
    assert row.modified_at == POSTED + edited
    assert await _extractions(db_session) == [("101-901", POSTED + edited, "queued")]


async def test_an_announcement_belongs_to_every_code_of_its_merged_course(db_session):
    await _store(db_session, _discussion(), course_codes=("CS1001301", "EE1001301"))
    assert (await _announcement(db_session)).course_codes == ["CS1001301", "EE1001301"]


async def test_one_answer_listing_a_discussion_twice_keeps_its_latest_version(db_session):
    late = _discussion(subject="Later words", edited=timedelta(minutes=3))
    assert await _store(db_session, late, _discussion()) == ["101-901"]
    assert (await _announcement(db_session)).subject == "Later words"


async def test_a_classifier_down_leaves_the_raw_post_for_a_later_tick(db_session, factory):
    await _store(db_session, _discussion())
    classifier = RecordingClassifier({"Class cancelled": CANCELLED})
    classifier.down = True

    assert await classify_pending(factory, classifier) == 0
    row = await _announcement(db_session)
    assert (row.route, row.raw_subject) == ("unclassified", "Class cancelled")

    classifier.down = False
    assert await classify_pending(factory, classifier) == 1
    assert (await _announcement(db_session)).route == "sent"


async def test_an_edit_while_the_classifier_answered_keeps_the_edit(db_session, factory):
    await _store(db_session, _discussion())

    class EditedMeanwhile(RecordingClassifier):
        async def classify(self, posts):
            results = await super().classify(posts)
            await _store(db_session, _discussion(edited=timedelta(minutes=1)))
            return results

    assert await classify_pending(factory, EditedMeanwhile({"Class cancelled": CANCELLED})) == 0
    row = await _announcement(db_session)
    assert (row.route, row.modified_at) == ("unclassified", POSTED + timedelta(minutes=1))
    assert await _extractions(db_session) == []


@pytest.mark.parametrize(
    ("scores", "route"),
    [
        ({"exam_prep": 0.10}, "sent"),
        ({"grade_release": 0.0999}, "not_sent"),
        ({"class_change": 0.8999}, "sent"),
        ({"exam_review": 0.90}, "not_sent"),
        ({"grade_release": 0.95}, "not_sent"),
        ({"class_cancel": 0.98}, "sent"),
        ({"exam_seating": 0.90}, "sent"),
        ({"materials": 0.5, "attendance": 0.2}, "not_sent"),
    ],
)
async def test_the_route_rule(scores, route):
    assert route_for(scores) == route


async def _fail_an_hour_ago(session, key: str = "101-901") -> None:
    await session.execute(
        update(CourseAnnouncement)
        .where(CourseAnnouncement.id == key)
        .values(classify_failed_at=func.now() - timedelta(hours=1, seconds=1))
    )
    await session.commit()


async def test_a_post_with_no_result_waits_an_hour(db_session, factory):
    await _store(db_session, _discussion())
    classifier = RecordingClassifier({"Class cancelled": CANCELLED})
    classifier.unanswered = {"Class cancelled"}

    assert await classify_pending(factory, classifier) == 0
    assert await classify_pending(factory, classifier) == 0
    assert len(classifier.posts) == 1

    classifier.unanswered = set()
    await _fail_an_hour_ago(db_session)
    assert await classify_pending(factory, classifier) == 1
    row = await _announcement(db_session)
    assert (row.route, row.classify_failed_at) == ("sent", None)


async def test_posts_that_had_no_result_go_after_the_others(db_session, factory):
    await _store(db_session, _discussion(1, subject="Too long"))
    classifier = RecordingClassifier()
    classifier.unanswered = {"Too long"}
    await classify_pending(factory, classifier)
    await _fail_an_hour_ago(db_session)
    await _store(db_session, _discussion(2, subject="Later", edited=timedelta(hours=1)))

    assert await classify_pending(factory, classifier, limit=1) == 1

    assert [p.subject for p in classifier.posts] == ["Too long", "Later"]


async def test_an_edit_of_a_post_with_no_result_is_classified_at_once(db_session, factory):
    await _store(db_session, _discussion(subject="Too long"))
    classifier = RecordingClassifier()
    classifier.unanswered = {"Too long"}
    await classify_pending(factory, classifier)

    await _store(db_session, _discussion(subject="Shorter", edited=timedelta(minutes=5)))

    assert await classify_pending(factory, classifier) == 1
    assert (await _announcement(db_session)).route == "not_sent"


async def test_the_unclassified_query_reads_its_index_in_order(factory, prepared_engine):
    asked: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "classify_failed_at" in statement:
            asked.append((statement, parameters))

    sa.event.listen(prepared_engine.sync_engine, "before_cursor_execute", record)
    try:
        await classify_pending(factory, RecordingClassifier())
    finally:
        sa.event.remove(prepared_engine.sync_engine, "before_cursor_execute", record)

    [(statement, parameters)] = asked
    async with prepared_engine.connect() as conn:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        await conn.exec_driver_sql("SET LOCAL enable_sort = off")
        plan = "\n".join(
            row[0] for row in await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
        )
        await conn.rollback()
    assert "ix_course_announcements_unclassified" in plan
    assert "Sort" not in plan
