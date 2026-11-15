"""The extraction job: one queued announcement a tick through the LLM, after
the bulletins, with its retries. Every announcement and answer here is
invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from server.bulletins.models import Bulletin
from server.course_announcements.extractor import (
    Extractor,
    queue_extraction,
    run_extraction_tick,
)
from server.course_announcements.ingest import classify_pending, store_announcements
from server.course_announcements.models import (
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
)
from server.db import build_session_factory
from server.scheduler.runtime import build_scheduler
from server.syncjobs.moodle_client import FetchedDiscussion
from server.tests.conftest import (
    RecordingClassifier,
    ScriptedLLM,
    extracted_change,
    extraction_answer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
SUBJECT = "停課通知"
CANCELLED = extraction_answer(labels=["class_cancel"], course_changes=[extracted_change()])


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _discussion(n: int = 1, *, edited: timedelta = timedelta(0), posted=POSTED):
    return FetchedDiscussion(
        discussion_id=100 + n,
        post_id=900 + n,
        subject=SUBJECT,
        message=f"<p>11/19（四）停課，第 {n} 則虛構公告。</p>",
        created_at=posted,
        modified_at=posted + edited,
        group_id=-1,
        visible_from=None,
        has_attachment=False,
        pinned=False,
        starred=False,
    )


@pytest.fixture
def factory(prepared_engine):
    return build_session_factory(prepared_engine)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def make_extractor(factory, test_settings, clock):
    def _make(llm: ScriptedLLM) -> Extractor:
        return Extractor(session_factory=factory, settings=test_settings, llm=llm, clock=clock)

    return _make


async def _queued(factory, *discussions) -> None:
    """Store and classify `discussions` as a cancellation, which queues each."""
    async with factory() as session:
        await store_announcements(
            session,
            term_code="1151",
            moodle_course_id=7001,
            course_codes=["CS1001301"],
            discussions=list(discussions or [_discussion()]),
        )
        await session.commit()
    await classify_pending(factory, RecordingClassifier({SUBJECT: {"class_cancel": 0.98}}))


async def _extractions(db_session) -> list[CourseAnnouncementExtraction]:
    rows = await db_session.scalars(
        select(CourseAnnouncementExtraction)
        .order_by(CourseAnnouncementExtraction.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def test_a_queued_announcement_is_extracted_with_its_model_and_answer(
    db_session, factory, make_extractor
):
    await _queued(factory)
    llm = ScriptedLLM(CANCELLED, model="gemma-test")

    assert await run_extraction_tick(make_extractor(llm)) is True

    (extraction,) = await _extractions(db_session)
    assert (extraction.status, extraction.model, extraction.certain) == ("done", "gemma-test", True)
    assert extraction.raw_output == CANCELLED
    assert (extraction.attempts, extraction.error_code) == (1, None)
    assert "11/19（四）停課，第 1 則虛構公告。" in llm.requests[0]["messages"][1]["content"]
    assert await run_extraction_tick(make_extractor(llm)) is False
    assert len(llm.requests) == 1


async def test_one_announcement_a_tick_the_latest_post_first(db_session, factory, make_extractor):
    await _queued(factory, _discussion(1), _discussion(2, posted=POSTED + timedelta(hours=1)))
    extractor = make_extractor(ScriptedLLM(CANCELLED))

    await run_extraction_tick(extractor)

    assert [(e.announcement_id, e.status) for e in await _extractions(db_session)] == [
        ("101-901", "queued"),
        ("102-902", "done"),
    ]


async def test_a_pending_bulletin_goes_first(db_session, factory, make_extractor):
    await _queued(factory)
    db_session.add(
        Bulletin(external_id="1", source_url="https://bulletin.test/1", title="Invented")
    )
    await db_session.commit()
    llm = ScriptedLLM(CANCELLED)

    assert await run_extraction_tick(make_extractor(llm)) is False

    assert llm.requests == []
    assert [e.status for e in await _extractions(db_session)] == ["queued"]


async def test_a_bulletin_at_its_attempt_limit_does_not_hold_extraction(
    db_session, factory, make_extractor, test_settings
):
    await _queued(factory)
    db_session.add_all(
        [
            Bulletin(
                external_id="1",
                source_url="https://bulletin.test/1",
                title="Invented",
                processing_attempts=test_settings.bulletin_max_process_attempts,
            ),
            Bulletin(
                external_id="2",
                source_url="https://bulletin.test/2",
                title="Invented",
                is_deleted=True,
            ),
        ]
    )
    await db_session.commit()

    assert await run_extraction_tick(make_extractor(ScriptedLLM(CANCELLED))) is True


class _EditingLLM(ScriptedLLM):
    """Stores an edit of the announcement while the LLM is asked."""

    def __init__(self, factory, *answers) -> None:
        super().__init__(*answers)
        self.factory = factory

    async def complete_json(self, **kwargs):
        async with self.factory() as session:
            await store_announcements(
                session,
                term_code="1151",
                moodle_course_id=7001,
                course_codes=["CS1001301"],
                discussions=[_discussion(edited=timedelta(minutes=5))],
            )
            await session.commit()
        return await super().complete_json(**kwargs)


async def test_an_edit_during_extraction_discards_the_answer_and_extracts_the_edit(
    db_session, factory, make_extractor, clock
):
    await _queued(factory)

    assert await run_extraction_tick(make_extractor(_EditingLLM(factory, CANCELLED))) is True

    (old,) = await _extractions(db_session)
    assert (old.status, old.raw_output, old.model) == ("superseded", None, None)

    await classify_pending(factory, RecordingClassifier({SUBJECT: {"class_cancel": 0.98}}))
    clock.now += timedelta(minutes=1)
    await run_extraction_tick(make_extractor(ScriptedLLM(CANCELLED)))

    old, new = await _extractions(db_session)
    assert (new.version, new.status) == (POSTED + timedelta(minutes=5), "done")


async def test_a_same_version_re_extract_supersedes_the_earlier_pending_items(
    db_session, factory, make_extractor, clock
):
    await _queued(factory)
    await run_extraction_tick(make_extractor(ScriptedLLM(extraction_answer())))
    (first,) = await _extractions(db_session)
    db_session.add_all(
        CourseAnnouncementItem(
            extraction_id=first.id, course_no="CS1001301", kind="course_change",
            action="add", payload={"kind": "cancelled"}, state=state,
        )
        for state in ("pending", "rejected")
    )
    await db_session.commit()

    async with factory() as session:
        assert await queue_extraction(session, "101-901") is True
        assert await queue_extraction(session, "101-901") is False
        await session.commit()
    clock.now += timedelta(minutes=1)
    await run_extraction_tick(make_extractor(ScriptedLLM(extraction_answer(), model="other")))

    first, second = await _extractions(db_session)
    assert (first.status, second.status, second.model) == ("done", "done", "other")
    states = await db_session.scalars(
        select(CourseAnnouncementItem.state)
        .where(CourseAnnouncementItem.extraction_id == first.id)
        .order_by(CourseAnnouncementItem.id)
        .execution_options(populate_existing=True)
    )
    assert states.all() == ["superseded", "rejected"]


async def test_an_unclassified_announcement_cannot_be_queued(db_session, factory):
    async with factory() as session:
        await store_announcements(
            session,
            term_code="1151",
            moodle_course_id=7001,
            course_codes=["CS1001301"],
            discussions=[_discussion()],
        )
        assert await queue_extraction(session, "101-901") is False
        assert await queue_extraction(session, "999-999") is False


async def test_a_claim_a_restart_cut_off_counts_as_a_failed_attempt(
    db_session, factory, make_extractor, clock
):
    await _queued(factory)
    await db_session.execute(
        update(CourseAnnouncementExtraction).values(
            status="running", attempts=1, updated_at=NOW - timedelta(minutes=10)
        )
    )
    await db_session.commit()
    llm = ScriptedLLM(CANCELLED)

    assert await run_extraction_tick(make_extractor(llm)) is False

    (extraction,) = await _extractions(db_session)
    assert (extraction.status, extraction.error_code, extraction.attempts) == (
        "queued",
        "abandoned",
        1,
    )
    assert extraction.next_attempt_at == NOW + timedelta(minutes=1)

    clock.now += timedelta(minutes=1)
    assert await run_extraction_tick(make_extractor(llm)) is True
    (extraction,) = await _extractions(db_session)
    assert (extraction.status, extraction.attempts) == ("done", 2)


async def test_a_live_claim_is_left_alone(db_session, factory, make_extractor):
    await _queued(factory)
    await db_session.execute(
        update(CourseAnnouncementExtraction).values(
            status="running", attempts=1, updated_at=NOW - timedelta(minutes=9)
        )
    )
    await db_session.commit()

    assert await run_extraction_tick(make_extractor(ScriptedLLM(CANCELLED))) is False

    (extraction,) = await _extractions(db_session)
    assert (extraction.status, extraction.error_code) == ("running", None)


async def test_failures_wait_1_4_and_16_ticks_then_fail_until_queued_again(
    db_session, factory, make_extractor, clock
):
    await _queued(factory)
    llm = ScriptedLLM(500)
    extractor = make_extractor(llm)
    for wait in (1, 4, 16):
        assert await run_extraction_tick(extractor) is True
        (extraction,) = await _extractions(db_session)
        assert (extraction.status, extraction.error_code) == ("queued", "http_status")
        assert extraction.next_attempt_at == clock.now + timedelta(minutes=wait)
        clock.now += timedelta(minutes=wait) - timedelta(seconds=1)
        assert await run_extraction_tick(extractor) is False
        clock.now += timedelta(seconds=1)

    assert await run_extraction_tick(extractor) is True
    (extraction,) = await _extractions(db_session)
    assert (extraction.status, extraction.attempts, extraction.next_attempt_at) == (
        "failed",
        4,
        None,
    )
    # Each attempt is the client's request and its two retries.
    assert len(llm.requests) == 12
    clock.now += timedelta(days=1)
    assert await run_extraction_tick(extractor) is False

    async with factory() as session:
        assert await queue_extraction(session, "101-901") is True
        await session.commit()
    assert await run_extraction_tick(make_extractor(ScriptedLLM(CANCELLED))) is True
    assert [e.status for e in await _extractions(db_session)] == ["failed", "done"]


class _SlowLLM(ScriptedLLM):
    """Fails after the clock has moved on by `takes`."""

    def __init__(self, clock: _Clock, takes: timedelta) -> None:
        super().__init__(500)
        self.clock, self.takes = clock, takes

    async def complete_json(self, **kwargs):
        self.clock.now += self.takes
        return await super().complete_json(**kwargs)


async def test_a_slow_failure_still_waits_its_ticks_after_it_failed(
    db_session, factory, make_extractor, clock
):
    await _queued(factory)
    started = clock.now

    await run_extraction_tick(make_extractor(_SlowLLM(clock, timedelta(minutes=6))))
    (extraction,) = await _extractions(db_session)
    # The first retry is the next tick, whenever the call ended.
    assert extraction.next_attempt_at == started + timedelta(minutes=6)

    started = clock.now
    await run_extraction_tick(make_extractor(_SlowLLM(clock, timedelta(minutes=6))))
    (extraction,) = await _extractions(db_session)
    assert extraction.next_attempt_at == started + timedelta(minutes=6 + 3)


async def test_text_a_column_cannot_hold_is_dropped_from_the_answer(
    db_session, factory, make_extractor
):
    await _queued(factory)
    makeup = {
        "date": "2026-11-21",
        "start_time": None,
        "end_time": None,
        "periods": None,
        "room": "IB-302\x00",
        "evidence": "11/19（四）停課\ud800",
    }
    answer = extraction_answer(
        labels=["class_cancel"],
        course_changes=[extracted_change(evidence="11/19（四）\x00停課")],
        makeup_classes=[makeup],
    )

    # Escaped, as a server sends them: a NUL and a lone surrogate.
    await run_extraction_tick(make_extractor(ScriptedLLM(json.dumps(answer))))

    (extraction,) = await _extractions(db_session)
    assert extraction.status == "done"
    assert extraction.raw_output["course_changes"][0]["evidence"] == "11/19（四）停課"
    item = (
        await db_session.scalars(
            select(CourseAnnouncementItem).where(CourseAnnouncementItem.kind == "makeup")
        )
    ).one()
    assert (item.payload["room"], item.evidence) == ("IB-302", "11/19（四）停課")


async def test_an_answer_that_fails_the_schema_logs_and_stores_a_code_alone(
    db_session, factory, make_extractor
):
    await _queued(factory)
    invalid = {**CANCELLED, "course_changes": [extracted_change(kind="moved")]}

    with capture_logs() as logs:
        await run_extraction_tick(make_extractor(ScriptedLLM(invalid)))

    (extraction,) = await _extractions(db_session)
    assert (extraction.error_code, extraction.raw_output) == ("invalid_answer", None)
    assert any(log["event"] == "course_announcements.extraction_failed" for log in logs)
    assert "停課" not in repr(logs) and "11/19" not in repr(logs)


@pytest.mark.parametrize(
    ("url", "scheduled"), [("http://classifier.test:40002", True), ("", False)]
)
async def test_extraction_is_scheduled_only_with_a_classifier(test_settings, url, scheduled):
    settings = test_settings.model_copy(update={"announcement_classifier_url": url})
    scheduler = build_scheduler(async_sessionmaker(), object(), settings, llm=object())

    job = scheduler.get_job("course_announcement_extract")
    assert (job is not None) == scheduled
    if scheduled:
        assert job.trigger.interval == timedelta(minutes=1)
