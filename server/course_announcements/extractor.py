"""The extraction job: every minute, while no bulletin waits for the LLM, one
queued extraction goes to it and its result is stored.

The LLM serves bulletins and extraction from one slot, so bulletins go
first and a tick sends one announcement. The claim commits before the LLM
is asked, and the answer is kept only if the extraction is still the one
running for the announcement's stored version.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import structlog
from sqlalchemy import exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.bulletins.jobs import claimable_bulletins
from server.bulletins.llm.base import LLMError
from server.bulletins.llm.openai_compat import OpenAICompatibleClient
from server.config import Settings
from server.course_announcements.extraction import build_extraction_llm, extract
from server.course_announcements.models import (
    EXTRACTION_LIVE_WHERE,
    AnnouncementRoute,
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
    ExtractionStatus,
    ItemState,
)
from server.db import session_scope

logger = structlog.get_logger(__name__)

TICK = timedelta(minutes=1)
# Ticks a failed attempt waits before the next; after the last, it has failed.
RETRY_TICKS = (1, 4, 16)
# Longer than the client's three attempts of llm_timeout_seconds (120 s) and
# their backoff, so a claim this old was cut off, as by a restart.
CLAIM_EXPIRES_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class Extractor:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    llm: OpenAICompatibleClient
    clock: Callable[[], datetime] = partial(datetime.now, UTC)


def build_extractor(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> Extractor:
    return Extractor(
        session_factory=session_factory, settings=settings, llm=build_extraction_llm(settings)
    )


class ExtractionTickFailed(Exception):
    """A tick failed. Carries the failure's type alone: a database error's
    message holds its statement's parameters, announcement text among them."""


@dataclass(frozen=True)
class _Claim:
    id: int
    announcement_id: str
    text: str
    posted_at: datetime
    term_code: str
    course_codes: list[str]


async def run_extraction_tick(extractor: Extractor) -> bool:
    """Count claims older than `CLAIM_EXPIRES_AFTER` as failed attempts, then
    extract one queued announcement unless a bulletin waits for the LLM.
    Returns whether one was sent."""
    now = extractor.clock()
    await _expire_claims(extractor.session_factory, now)
    if await _bulletin_waiting(extractor):
        return False
    claim = await _claim(extractor.session_factory, now)
    if claim is None:
        return False
    try:
        output = await extract(
            extractor.llm,
            text=claim.text,
            posted_at=claim.posted_at,
            term_code=claim.term_code,
            course_codes=claim.course_codes,
        )
    except LLMError as exc:
        logger.warning(
            "course_announcements.extraction_failed",
            extraction_id=claim.id,
            error=exc.code,
            status=exc.status,
        )
        await _attempt_failed(extractor.session_factory, claim.id, exc.code, now)
        return True
    failed: str | None = None
    try:
        await store_result(extractor, claim.id, output, now)
    except Exception as exc:
        failed = type(exc).__name__
    if failed is not None:
        await _attempt_failed(extractor.session_factory, claim.id, "store_failed", now)
        # Raised outside the handler, so the scheduler's log has no cause to print.
        raise ExtractionTickFailed(failed)
    return True


async def queue_extraction(session: AsyncSession, announcement_id: str) -> bool:
    """Queue the announcement's stored version for extraction, as an operator
    asks; a version already queued or running stays as it is. Returns
    whether one was queued."""
    queued = await session.scalar(
        pg_insert(CourseAnnouncementExtraction)
        .from_select(
            ["announcement_id", "version"],
            select(CourseAnnouncement.id, CourseAnnouncement.modified_at).where(
                CourseAnnouncement.id == announcement_id,
                CourseAnnouncement.route != AnnouncementRoute.unclassified.value,
            ),
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
    return queued is not None


async def _bulletin_waiting(extractor: Extractor) -> bool:
    async with session_scope(extractor.session_factory) as session:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        *claimable_bulletins(extractor.settings.bulletin_max_process_attempts)
                    )
                )
            )
        )


async def _claim(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> _Claim | None:
    """The queued extraction due now of the latest post, marked running."""
    extraction, announcement = CourseAnnouncementExtraction, CourseAnnouncement
    async with session_scope(session_factory) as session:
        row = (
            await session.execute(
                select(
                    extraction.id,
                    extraction.announcement_id,
                    announcement.text,
                    announcement.posted_at,
                    announcement.term_code,
                    announcement.course_codes,
                )
                .join(announcement, announcement.id == extraction.announcement_id)
                .where(
                    extraction.status == ExtractionStatus.queued.value,
                    or_(extraction.next_attempt_at.is_(None), extraction.next_attempt_at <= now),
                    extraction.version == announcement.modified_at,
                    announcement.text.is_not(None),
                )
                .order_by(announcement.posted_at.desc(), extraction.id)
                .limit(1)
                .with_for_update(of=extraction, skip_locked=True)
            )
        ).first()
        if row is None:
            return None
        await session.execute(
            update(extraction)
            .where(extraction.id == row.id)
            .values(
                status=ExtractionStatus.running.value,
                attempts=extraction.attempts + 1,
                next_attempt_at=None,
                # When it was claimed, which `_expire_claims` reads.
                updated_at=now,
            )
        )
    return _Claim(
        id=row.id,
        announcement_id=row.announcement_id,
        text=row.text,
        posted_at=row.posted_at,
        term_code=row.term_code,
        course_codes=list(row.course_codes),
    )


async def _expire_claims(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> None:
    async with session_scope(session_factory) as session:
        stale = await session.scalars(
            select(CourseAnnouncementExtraction.id)
            .where(
                CourseAnnouncementExtraction.status == ExtractionStatus.running.value,
                CourseAnnouncementExtraction.updated_at <= now - CLAIM_EXPIRES_AFTER,
            )
            .order_by(CourseAnnouncementExtraction.id)
            .with_for_update(skip_locked=True)
        )
        for extraction_id in stale.all():
            logger.warning("course_announcements.extraction_abandoned", extraction_id=extraction_id)
            await _record_failure(session, extraction_id, "abandoned", now)


async def _attempt_failed(
    session_factory: async_sessionmaker[AsyncSession],
    extraction_id: int,
    code: str,
    now: datetime,
) -> None:
    async with session_scope(session_factory) as session:
        await _record_failure(session, extraction_id, code, now)


async def _record_failure(
    session: AsyncSession, extraction_id: int, code: str, now: datetime
) -> None:
    """Queue the running extraction for its next attempt, or fail it after
    the last; one superseded meanwhile stays superseded."""
    row = await session.get(
        CourseAnnouncementExtraction, extraction_id, with_for_update=True, populate_existing=True
    )
    if row is None or row.status != ExtractionStatus.running.value:
        return
    row.error_code = code
    if row.attempts <= len(RETRY_TICKS):
        row.status = ExtractionStatus.queued.value
        row.next_attempt_at = now + TICK * RETRY_TICKS[row.attempts - 1]
    else:
        row.status = ExtractionStatus.failed.value
        row.next_attempt_at = None


async def store_result(
    extractor: Extractor, extraction_id: int, output: dict[str, Any], now: datetime
) -> bool:
    """Store `output` if the extraction is still running and its announcement
    still at that version, superseding what earlier extractions of the
    announcement left pending. Returns whether it was stored."""
    async with session_scope(extractor.session_factory) as session:
        row = await session.get(
            CourseAnnouncementExtraction,
            extraction_id,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None:
            return False
        # A share lock, so an edit stored meanwhile waits and then supersedes this.
        announcement = await session.get(
            CourseAnnouncement,
            row.announcement_id,
            with_for_update={"read": True},
            populate_existing=True,
        )
        if (
            announcement is None
            or row.status != ExtractionStatus.running.value
            or row.version != announcement.modified_at
        ):
            logger.info("course_announcements.extraction_discarded", extraction_id=extraction_id)
            return False
        row.status = ExtractionStatus.done.value
        row.model = extractor.llm.model
        row.raw_output = output
        row.certain = output["certain"]
        row.error_code = None
        earlier = select(CourseAnnouncementExtraction.id).where(
            CourseAnnouncementExtraction.announcement_id == announcement.id,
            CourseAnnouncementExtraction.id < row.id,
        )
        await session.execute(
            update(CourseAnnouncementItem)
            .where(
                CourseAnnouncementItem.extraction_id.in_(earlier),
                CourseAnnouncementItem.state == ItemState.pending.value,
            )
            .values(state=ItemState.superseded.value)
        )
    return True
