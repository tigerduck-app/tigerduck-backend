"""The course announcement poll: every minute, the news forums of the
current term's Moodle courses that are due, most overdue first, as many as
its own request budget allows, then classification of what is stored.

A course is polled with the token of one eligible holder: a user enrolled
in it (`user_moodle_courses`) with an active Moodle credential and a device
the sync worker serves. A token Moodle rejects, or a course it refuses,
moves the course to the next holder; marking the credential invalid stays
with the user's sync job, which checks the rejected token is still the
stored one.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial

import sqlalchemy as sa
import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from server.academic_calendar.terms import current_term_code
from server.auth.crypto import CredentialCipher
from server.auth.models import CredentialStatus, ExternalAccount
from server.auth.service import PROVIDER_NTUST_SSO
from server.config import Settings
from server.course_announcements.classifier import Classifier, HttpClassifier
from server.course_announcements.ingest import classify_pending, store_announcements
from server.course_announcements.models import CourseAnnouncementCourse
from server.db import session_scope
from server.metrics import observe_course_announcement_moodle_request
from server.syncjobs.assignments import course_nos
from server.syncjobs.availability import is_moodle_available
from server.syncjobs.budget import RequestBudget
from server.syncjobs.credentials import CredentialInvalid, load_moodle_token
from server.syncjobs.devices import has_syncing_device
from server.syncjobs.executor import SyncWorker
from server.syncjobs.models import UserMoodleCourse
from server.syncjobs.moodle_client import (
    FetchedCourse,
    FetchedDiscussion,
    ForumFetcher,
    HttpForumFetcher,
    MoodleRateLimited,
    MoodleRefused,
    MoodleTokenInvalid,
    MoodleUnavailable,
)

logger = structlog.get_logger(__name__)

# A course is due again this long after a poll, by the age of its latest post.
ACTIVE_INTERVAL = timedelta(minutes=15)  # under 7 days
RECENT_INTERVAL = timedelta(hours=3)  # 7 to 14 days
QUIET_INTERVAL = timedelta(hours=12)  # beyond 14 days
SILENT_INTERVAL = timedelta(days=1)  # no post at all
ACTIVE_FOR = timedelta(days=7)
RECENT_FOR = timedelta(days=14)
# Courses one forum lookup asks for; their ids ride in the request's URL.
FORUM_LOOKUP_COURSES = 50


@dataclass(frozen=True)
class CourseAnnouncementPoller:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: CredentialCipher
    # Sends through `budget`'s event hooks.
    fetcher: ForumFetcher
    classifier: Classifier
    budget: RequestBudget
    # The sync worker's: both share one Moodle limit.
    sync_budget: RequestBudget
    clock: Callable[[], datetime] = partial(datetime.now, UTC)


def build_poller(settings: Settings, sync_worker: SyncWorker) -> CourseAnnouncementPoller:
    """The poll beside `sync_worker`, whose sessions, cipher and budget it
    shares, on a budget of its own."""
    budget = RequestBudget(
        settings.course_announcement_requests_per_minute,
        observe=observe_course_announcement_moodle_request,
    )
    return CourseAnnouncementPoller(
        session_factory=sync_worker.session_factory,
        settings=settings,
        cipher=sync_worker.cipher,
        fetcher=HttpForumFetcher(
            base_url=settings.moodle_base_url,
            timeout_seconds=settings.moodle_fetch_timeout_seconds,
            event_hooks=budget.event_hooks,
        ),
        classifier=HttpClassifier(settings.announcement_classifier_url),
        budget=budget,
        sync_budget=sync_worker.budget,
    )


class _Outcome(enum.Enum):
    polled = enum.auto()
    stop = enum.auto()


def poll_interval(latest_post_at: datetime | None, now: datetime) -> timedelta:
    if latest_post_at is None:
        return SILENT_INTERVAL
    age = now - latest_post_at
    if age < ACTIVE_FOR:
        return ACTIVE_INTERVAL
    if age <= RECENT_FOR:
        return RECENT_INTERVAL
    return QUIET_INTERVAL


def _in_term(idnumber: InstrumentedAttribute[str], term: str) -> sa.ColumnElement[bool]:
    """`moodle_semester(idnumber) == term`, for a term code."""
    return sa.and_(func.length(idnumber) > 4, func.upper(func.left(idnumber, 4)) == term)


def _eligible(settings: Settings, now: datetime) -> sa.ColumnElement[bool]:
    """Whether the `user_moodle_courses` row's user may lend their token."""
    return sa.and_(
        sa.exists().where(
            ExternalAccount.user_id == UserMoodleCourse.user_id,
            ExternalAccount.provider == PROVIDER_NTUST_SSO,
            ExternalAccount.credential_status == CredentialStatus.active.value,
        ),
        has_syncing_device(settings, now, UserMoodleCourse.user_id),
    )


def _paused(poller: CourseAnnouncementPoller) -> bool:
    return poller.budget.paused or poller.sync_budget.paused


class PollTickFailed(Exception):
    """A tick failed. Carries the failure's type alone: a database error's
    message holds its statement's parameters, announcement text among them."""


async def run_poll_tick(poller: CourseAnnouncementPoller) -> int:
    """Poll the due courses, then classify every stored announcement still
    waiting for it, whether or not Moodle may be polled. Returns the
    courses polled."""
    now = poller.clock()
    failed: str | None = None
    polled = 0
    try:
        polled = await _poll_due_courses(poller, now)
    except Exception as exc:
        failed = type(exc).__name__
    try:
        await classify_pending(poller.session_factory, poller.classifier)
    except Exception as exc:
        failed = failed or type(exc).__name__
    if failed is not None:
        # Raised outside the handlers, so the scheduler's log has no cause to print.
        raise PollTickFailed(failed)
    return polled


async def _poll_due_courses(poller: CourseAnnouncementPoller, now: datetime) -> int:
    if _paused(poller):
        return 0
    async with session_scope(poller.session_factory) as session:
        available, _ = await is_moodle_available(session, poller.settings, now)
        term = await current_term_code(session, now) if available else None
        if term is None:
            return 0
        due = await _due_courses(session, poller.settings, term, now)
    polled = 0
    for course_id in due:
        if _paused(poller):
            break
        if await _poll_course(poller, course_id, term, now) is _Outcome.stop:
            break
        polled += 1
    return polled


async def _due_courses(
    session: AsyncSession, settings: Settings, term: str, now: datetime
) -> list[int]:
    """The term's courses with an eligible holder that are due, never polled
    first, then the longest overdue, as many as a minute's budget."""
    with_holder = (
        select(UserMoodleCourse.moodle_course_id)
        .where(_in_term(UserMoodleCourse.idnumber, term), _eligible(settings, now))
        .distinct()
        .subquery()
    )
    state = CourseAnnouncementCourse
    rows = await session.scalars(
        select(with_holder.c.moodle_course_id)
        .outerjoin(state, state.moodle_course_id == with_holder.c.moodle_course_id)
        .where(sa.or_(state.next_due_at.is_(None), state.next_due_at <= now))
        .order_by(state.next_due_at.asc().nulls_first(), with_holder.c.moodle_course_id)
        .limit(settings.course_announcement_requests_per_minute)
    )
    return list(rows)


@dataclass(frozen=True)
class _Course:
    moodle_course_id: int
    codes: list[str]
    forum_id: int | None
    latest_post_at: datetime | None
    # Eligible holders, the one to try first leading.
    holders: list[uuid.UUID]


def _starting_at(
    holders: Sequence[uuid.UUID], first: uuid.UUID | None
) -> list[uuid.UUID]:
    """`holders` in id order from `first`, or from the next one after it."""
    ordered = sorted(holders)
    if first is None:
        return ordered
    at = next((n for n, holder in enumerate(ordered) if holder >= first), 0)
    return ordered[at:] + ordered[:at]


async def _load_course(
    session: AsyncSession, settings: Settings, course_id: int, now: datetime
) -> _Course | None:
    state = await session.get(CourseAnnouncementCourse, course_id)
    holders = await session.scalars(
        select(UserMoodleCourse.user_id).where(
            UserMoodleCourse.moodle_course_id == course_id, _eligible(settings, now)
        )
    )
    listed = (
        await session.execute(
            select(UserMoodleCourse.idnumber, UserMoodleCourse.full_name)
            .where(UserMoodleCourse.moodle_course_id == course_id)
            .order_by(UserMoodleCourse.last_seen_at.desc())
            .limit(1)
        )
    ).first()
    if listed is None:
        return None
    codes = course_nos(
        FetchedCourse(
            moodle_course_id=course_id,
            short_name=None,
            full_name=listed.full_name,
            category_id=None,
            enrolled_user_count=None,
            idnumber=listed.idnumber,
        )
    )
    return _Course(
        moodle_course_id=course_id,
        codes=codes,
        forum_id=state.forum_id if state else None,
        latest_post_at=state.latest_post_at if state else None,
        holders=_starting_at(list(holders), state.holder_user_id if state else None),
    )


async def _poll_course(
    poller: CourseAnnouncementPoller, course_id: int, term: str, now: datetime
) -> _Outcome:
    checked_at = poller.clock()
    async with session_scope(poller.session_factory) as session:
        available, _ = await is_moodle_available(session, poller.settings, checked_at)
        if not available:
            return _Outcome.stop
        course = await _load_course(session, poller.settings, course_id, checked_at)
        if course is None:
            return _Outcome.polled
        holder, token = None, None
        for candidate in course.holders:
            try:
                token = await load_moodle_token(session, poller.cipher, user_id=candidate)
            except CredentialInvalid:
                continue
            holder = candidate
            break
        unknown_forums = (
            await _courses_without_forum(session, holder, term, course_id)
            if holder is not None and course.forum_id is None
            else []
        )
    if holder is None or token is None:
        await _record_poll(poller, course, term, now, holder=None, discussions=[])
        return _Outcome.polled

    next_holder = course.holders[(course.holders.index(holder) + 1) % len(course.holders)]
    forums: dict[int, int] = {}
    try:
        if course.forum_id is None:
            forums = await poller.fetcher.fetch_news_forums(
                token=token, course_ids=unknown_forums
            )
        forum_id = course.forum_id or forums.get(course_id)
        discussions = (
            await poller.fetcher.fetch_discussions(token=token, forum_id=forum_id)
            if forum_id is not None
            else None
        )
    except (MoodleTokenInvalid, MoodleRefused) as exc:
        logger.info(
            "course_announcements.holder_refused",
            moodle_course_id=course_id,
            error=str(exc),
        )
        if isinstance(exc, MoodleRefused):
            # Moodle refuses a forum deleted since it was stored (get_forum_discussions,
            # moodle/moodle mod/forum/externallib.php), so the next poll looks it up again.
            course = replace(course, forum_id=None)
            forums = {other: forum for other, forum in forums.items() if other != course_id}
        await _record_poll(
            poller, course, term, now, holder=next_holder, discussions=[], forums=forums
        )
        return _Outcome.polled
    except MoodleRateLimited:
        pause = poller.settings.sync_job_backoff_base_seconds
        poller.budget.pause(pause)
        poller.sync_budget.pause(pause)
        logger.warning("course_announcements.rate_limited_pause", seconds=pause)
        return _Outcome.stop
    except MoodleUnavailable as exc:
        logger.warning("course_announcements.moodle_unavailable", error=str(exc))
        return _Outcome.stop
    if discussions is None:
        # No news forum this holder sees: maybe one who can sees it.
        await _record_poll(
            poller, course, term, now, holder=next_holder, discussions=[], forums=forums
        )
        return _Outcome.polled
    await _record_poll(
        poller,
        course,
        term,
        now,
        holder=holder,
        discussions=discussions,
        forums={**forums, course_id: forum_id},
    )
    return _Outcome.polled


async def _courses_without_forum(
    session: AsyncSession, holder: uuid.UUID, term: str, course_id: int
) -> list[int]:
    """`course_id` and more of the holder's courses this term whose news
    forum is not known, for one lookup."""
    known = select(CourseAnnouncementCourse.moodle_course_id).where(
        CourseAnnouncementCourse.forum_id.is_not(None)
    )
    others = await session.scalars(
        select(UserMoodleCourse.moodle_course_id)
        .where(
            UserMoodleCourse.user_id == holder,
            UserMoodleCourse.moodle_course_id != course_id,
            _in_term(UserMoodleCourse.idnumber, term),
            UserMoodleCourse.moodle_course_id.not_in(known),
        )
        .order_by(UserMoodleCourse.moodle_course_id)
        .limit(FORUM_LOOKUP_COURSES - 1)
    )
    return [course_id, *others]


async def _record_poll(
    poller: CourseAnnouncementPoller,
    course: _Course,
    term: str,
    now: datetime,
    *,
    holder: uuid.UUID | None,
    discussions: Sequence[FetchedDiscussion],
    forums: dict[int, int] | None = None,
) -> None:
    """Store what one poll read and when the course is due again, in one
    transaction."""
    latest = max(
        [course.latest_post_at, *(d.created_at for d in discussions)],
        key=lambda moment: moment or datetime.min.replace(tzinfo=UTC),
    )
    async with session_scope(poller.session_factory) as session:
        for other, forum_id in (forums or {}).items():
            if other != course.moodle_course_id:
                await session.execute(
                    pg_insert(CourseAnnouncementCourse)
                    .values(moodle_course_id=other, term_code=term, forum_id=forum_id)
                    .on_conflict_do_update(
                        index_elements=[CourseAnnouncementCourse.moodle_course_id],
                        set_={"forum_id": forum_id, "updated_at": func.now()},
                    )
                )
        values = {
            "term_code": term,
            "course_codes": course.codes,
            "forum_id": (forums or {}).get(course.moodle_course_id, course.forum_id),
            "latest_post_at": latest,
            "next_due_at": now + poll_interval(latest, now),
            "holder_user_id": holder,
        }
        await session.execute(
            pg_insert(CourseAnnouncementCourse)
            .values(moodle_course_id=course.moodle_course_id, **values)
            .on_conflict_do_update(
                index_elements=[CourseAnnouncementCourse.moodle_course_id],
                set_={**values, "updated_at": func.now()},
            )
        )
        if discussions:
            await store_announcements(
                session,
                term_code=term,
                moodle_course_id=course.moodle_course_id,
                course_codes=course.codes,
                discussions=discussions,
            )
