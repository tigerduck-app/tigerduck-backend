"""Store course announcements, classify them and route each version.

The poll and the history import both store through `store_announcements`,
which keeps the version with the latest `modified_at` whichever arrives
first, and `classify_pending` classifies what is stored, on every poll tick,
so an announcement stored while the classifier was down needs no second
Moodle request. Only the current term's versions are queued for extraction,
so the history the import stores never reaches the LLM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import structlog
import sqlalchemy as sa
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.terms import current_term_code
from server.course_announcements.classifier import (
    MAX_POSTS,
    Classification,
    Classifier,
    ClassifierError,
    Post,
)
from server.course_announcements.items import refresh_contradictions
from server.course_announcements.models import (
    EXTRACTION_LIVE_WHERE,
    SUPERSEDABLE_ITEM_STATES,
    AnnouncementRoute,
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
    ExtractionStatus,
    ItemState,
)
from server.db import INSERT_CHUNK_ROWS, in_array, session_scope
from server.syncjobs.moodle_client import FetchedDiscussion

logger = structlog.get_logger(__name__)

# Any of these scoring in [0.10, 0.90) sends a version to the LLM.
UNCERTAIN_LABELS = (
    "exam_info",
    "exam_change",
    "exam_seating",
    "exam_review",
    "exam_prep",
    "grade_release",
    "class_cancel",
    "class_change",
)
# Any of these scoring 0.90 or more sends it too: its facts are extracted.
FACT_LABELS = ("class_cancel", "class_change", "exam_info", "exam_change", "exam_seating")
UNCERTAIN_FROM = 0.10
CONFIDENT_FROM = 0.90

# Announcements classified in one tick, at most.
CLASSIFY_PER_TICK = 4 * MAX_POSTS
# A version the service answered with no result is sent again after this.
CLASSIFY_RETRY_AFTER = timedelta(hours=1)


def route_for(scores: Mapping[str, float]) -> AnnouncementRoute:
    uncertain = any(
        UNCERTAIN_FROM <= scores.get(label, 0.0) < CONFIDENT_FROM
        for label in UNCERTAIN_LABELS
    )
    confident_fact = any(scores.get(label, 0.0) >= CONFIDENT_FROM for label in FACT_LABELS)
    return AnnouncementRoute.sent if uncertain or confident_fact else AnnouncementRoute.not_sent


def announcement_id(discussion: FetchedDiscussion) -> str:
    return f"{discussion.discussion_id}-{discussion.post_id}"


async def store_announcements(
    session: AsyncSession,
    *,
    term_code: str,
    moodle_course_id: int,
    course_codes: Sequence[str],
    discussions: Sequence[FetchedDiscussion],
) -> list[str]:
    """Store each discussion's first post, unclassified, unless the stored
    version is as late or later, and retire what older versions left
    waiting. Returns the ids stored."""
    latest: dict[str, FetchedDiscussion] = {}
    for discussion in discussions:
        key = announcement_id(discussion)
        if key not in latest or discussion.modified_at > latest[key].modified_at:
            latest[key] = discussion
    rows = [
        {
            "id": key,
            "term_code": term_code,
            "moodle_course_id": moodle_course_id,
            "course_codes": list(course_codes),
            "raw_subject": d.subject,
            "raw_message": d.message,
            "subject": d.subject,
            "posted_at": d.created_at,
            "modified_at": d.modified_at,
            "group_id": d.group_id,
            "visible_from": d.visible_from,
            "has_attachment": d.has_attachment,
        }
        # In id order, so two writers storing the same rows lock them alike.
        for key, d in sorted(latest.items())
    ]
    stored: list[str] = []
    for start in range(0, len(rows), INSERT_CHUNK_ROWS):
        chunk = rows[start : start + INSERT_CHUNK_ROWS]
        insert = pg_insert(CourseAnnouncement).values(chunk)
        new = insert.excluded
        upsert = insert.on_conflict_do_update(
            index_elements=[CourseAnnouncement.id],
            set_={
                "term_code": new.term_code,
                "moodle_course_id": new.moodle_course_id,
                "course_codes": new.course_codes,
                "raw_subject": new.raw_subject,
                "raw_message": new.raw_message,
                "subject": new.subject,
                "text": None,
                "posted_at": new.posted_at,
                "modified_at": new.modified_at,
                "group_id": new.group_id,
                "visible_from": new.visible_from,
                "has_attachment": new.has_attachment,
                "classifier_release": None,
                "scores": None,
                "labels": None,
                "route": AnnouncementRoute.unclassified.value,
                "classify_failed_at": None,
                "updated_at": func.now(),
            },
            where=new.modified_at > CourseAnnouncement.modified_at,
        ).returning(CourseAnnouncement.id)
        stored += (await session.scalars(upsert)).all()
    if stored:
        await _retire_older_versions(session, stored)
        await refresh_contradictions(session, term_code, course_codes)
    return stored


async def _retire_older_versions(session: AsyncSession, ids: Sequence[str]) -> None:
    """Supersede the unfinished extractions and the pending and informational
    items of every version before the stored one, whatever the stored one's
    route."""
    older = (
        select(CourseAnnouncementExtraction.id)
        .join(
            CourseAnnouncement,
            CourseAnnouncement.id == CourseAnnouncementExtraction.announcement_id,
        )
        .where(
            in_array(CourseAnnouncementExtraction.announcement_id, ids),
            CourseAnnouncementExtraction.version < CourseAnnouncement.modified_at,
        )
    )
    await session.execute(
        update(CourseAnnouncementItem)
        .where(
            CourseAnnouncementItem.extraction_id.in_(older),
            CourseAnnouncementItem.state.in_(SUPERSEDABLE_ITEM_STATES),
        )
        .values(state=ItemState.superseded.value)
    )
    await session.execute(
        update(CourseAnnouncementExtraction)
        .where(
            CourseAnnouncementExtraction.id.in_(older),
            CourseAnnouncementExtraction.status.in_(
                [ExtractionStatus.queued.value, ExtractionStatus.running.value]
            ),
        )
        .values(status=ExtractionStatus.superseded.value)
    )


async def classify_pending(
    session_factory: async_sessionmaker[AsyncSession],
    classifier: Classifier,
    *,
    limit: int = CLASSIFY_PER_TICK,
) -> int:
    """Classify stored versions that are not yet, oldest first and those the
    service gave no result last, and queue an extraction for each of the
    current term the route rule sends. A version with no result waits
    CLASSIFY_RETRY_AFTER. No transaction is open while the service answers;
    a version replaced meanwhile keeps the newer one. Returns how many were
    classified."""
    async with session_scope(session_factory) as session:
        pending = (
            await session.execute(
                select(
                    CourseAnnouncement.id,
                    CourseAnnouncement.modified_at,
                    CourseAnnouncement.raw_subject,
                    CourseAnnouncement.raw_message,
                )
                .where(
                    CourseAnnouncement.route == AnnouncementRoute.unclassified.value,
                    sa.or_(
                        CourseAnnouncement.classify_failed_at.is_(None),
                        CourseAnnouncement.classify_failed_at
                        <= func.now() - CLASSIFY_RETRY_AFTER,
                    ),
                )
                .order_by(
                    CourseAnnouncement.classify_failed_at.asc().nulls_first(),
                    CourseAnnouncement.posted_at,
                    CourseAnnouncement.id,
                )
                .limit(limit)
            )
        ).all()
    if not pending:
        return 0
    try:
        info = await classifier.info()
        results = await classifier.classify(
            [Post(subject=row.raw_subject, message=row.raw_message) for row in pending]
        )
    except ClassifierError as exc:
        logger.warning("course_announcements.classifier_failed", error=str(exc))
        return 0
    classified = 0
    async with session_scope(session_factory) as session:
        term = await current_term_code(session, datetime.now(UTC))
        for row, result in zip(pending, results, strict=True):
            if result is None:
                logger.warning("course_announcements.no_classification", announcement_id=row.id)
                await session.execute(
                    _unclassified_version(row.id, row.modified_at).values(
                        classify_failed_at=func.now()
                    )
                )
                continue
            if await _store_classification(
                session, row.id, row.modified_at, result, info.release, term
            ):
                classified += 1
    return classified


def _unclassified_version(key: str, version: datetime) -> sa.Update:
    """An update of `key` if `version` is still its stored, unclassified one."""
    return update(CourseAnnouncement).where(
        CourseAnnouncement.id == key,
        CourseAnnouncement.modified_at == version,
        CourseAnnouncement.route == AnnouncementRoute.unclassified.value,
    )


async def _store_classification(
    session: AsyncSession,
    key: str,
    version: datetime,
    result: Classification,
    release: str | None,
    term: str | None,
) -> bool:
    """Store `result` on the version it was asked for, if that is still the
    stored, unclassified one, and queue it if it is of `term`."""
    route = route_for(result.scores)
    stored = await session.scalar(
        _unclassified_version(key, version)
        .values(
            text=result.text,
            scores=result.scores,
            labels=list(result.labels),
            classifier_release=release,
            route=route.value,
            raw_subject=None,
            raw_message=None,
            classify_failed_at=None,
        )
        .returning(CourseAnnouncement.id)
    )
    if stored is None:
        return False
    await queue_extractions(session, term, CourseAnnouncement.id == key)
    return True


async def queue_extractions(
    session: AsyncSession, term: str | None, *where: sa.ColumnElement[bool]
) -> int:
    """Queue an extraction of the stored version of each announcement of
    `term` that `where` picks and the route rule sends, unless one is queued
    or running already. Returns how many were queued."""
    if term is None:
        return 0
    queued = await session.scalars(
        pg_insert(CourseAnnouncementExtraction)
        .from_select(
            ["announcement_id", "version"],
            select(CourseAnnouncement.id, CourseAnnouncement.modified_at)
            .where(
                CourseAnnouncement.term_code == term,
                CourseAnnouncement.route == AnnouncementRoute.sent.value,
                *where,
            )
            # Waits for a store of a newer version to commit, then reads that
            # version, so its supersede step cannot miss what is queued here.
            # In id order, as store_announcements locks rows.
            .order_by(CourseAnnouncement.id)
            .with_for_update(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                CourseAnnouncementExtraction.announcement_id,
                CourseAnnouncementExtraction.version,
            ],
            index_where=text(EXTRACTION_LIVE_WHERE),
        )
        .returning(CourseAnnouncementExtraction.id)
    )
    return len(queued.all())
