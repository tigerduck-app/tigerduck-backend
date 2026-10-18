"""Check an assignment is still open right before reminding of it.

An assignment reminder (channel `assignment`, scenarios `reminder_*`) or the
start of an assignment's countdown Live Activity (`assignmentUrgent` on the
schedule channel) that reaches a student who already submitted tells them to
do something they did. The rows the reminder scan reads can be hours behind
Moodle: the submission probe runs only with an assignment sync, and an app
reports a submission only when it next uploads. So the pipeline asks
again right before a job's first delivery, the database first and Moodle
when the database cannot settle it, and acts on the verdict:

* `submitted`: a row of the assignment says so, or Moodle does. The job is
  cancelled and the rest brought in line as the probe does
  (`record_submitted`).
* `gone`: the job is no longer one to send. No row is left; a done, ignored
  or archived mark or an ignored course; a moved or passed due time; or
  Moodle says the student cannot submit, or has an extension past it.
* `send`: Moodle says the assignment is still open.
* `unverified`: nobody could say. Nothing is sent: the job is retried for a
  while, then cancelled, since a missed reminder is better than a wrong one.

No transaction is open while Moodle is asked, the rule the probe keeps too
(`syncjobs/executor.py`, `_refresh_submission_status`). The job stays claimed
as `processing` under the worker's id meanwhile, and the pipeline re-checks
that claim when it locks the job to act on the verdict.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import PushDelivery, PushJob, PushJobStatus
from server.auth.schemas import ScheduleScenario
from server.db import session_scope
from server.push.dedupe import SCHEDULE_CHANNEL, schedule_prefix
from server.push.payload import _to_unix_seconds
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import (
    EXCLUDED_LOCAL_STATUSES,
    ignored_courses,
    in_ignored_course,
)
from server.push.submission_cancel import cancel_for_submitted
from server.push.sync_trigger import enqueue_sync_trigger
from server.sync.models import UserAssignment, UserAssignmentOverride
from server.syncjobs.credentials import CredentialInvalid, load_moodle_token
from server.syncjobs.moodle_client import (
    FetchedSubmission,
    MoodleRateLimited,
    MoodleRefused,
    MoodleTokenInvalid,
    MoodleUnavailable,
    SubmissionStatusChecker,
    _without_token,
)
from server.syncjobs.submissions import apply_submission_status

if TYPE_CHECKING:
    from server.push.pipeline import PushPipelineWorker

logger = structlog.get_logger(__name__)

Verdict = Literal["send", "submitted", "gone", "unverified"]


@dataclass(frozen=True)
class PresendCheck:
    verdict: Verdict
    # For the log: a fixed code, never a value read from the job or Moodle.
    reason: str
    moodle_assignment_id: int | None = None
    submitted_at: datetime | None = None


@dataclass
class MoodleBreaker:
    """Whether Moodle failed for everyone during this push tick.

    The first check that fails the way every other check would too (no
    connection, a timeout, HTTP 5xx, a body that is not JSON, or rate
    limiting) opens it, and the tick's remaining checks come out
    `unverified` without asking: an outage then costs one failed call a
    tick, not one a job. Moodle's own refusal in a JSON answer, such as a
    dead token or a suspended account, is one user's and leaves it shut.
    """

    opened_by: str | None = None


def needs_check(job: PushJob) -> bool:
    """Whether `job` is one this check guards: an assignment reminder, or the
    start of an assignment's countdown that `/schedule/sync` filed, never its
    end. The portal's test start, under a `portal_la:` key and a made-up
    source id, names no assignment and goes out as it is."""
    if job.channel == REMINDER_CHANNEL:
        return job.scenario.startswith("reminder_")
    return (
        job.channel == SCHEDULE_CHANNEL
        and job.scenario == ScheduleScenario.assignment_urgent.value
        and (job.payload or {}).get("kind") != "live_activity_end"
        and job.device_id is not None
        and job.dedupe_key.startswith(schedule_prefix(job.device_id))
    )


async def check_before_send(
    worker: PushPipelineWorker, *, job_id: int, breaker: MoodleBreaker
) -> PresendCheck | None:
    """The verdict on claimed job `job_id`, or None when the check does not
    apply: a job of another kind, one no longer claimed by this worker, or a
    retry round, whose push may already have reached a device."""
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        job = await session.get(PushJob, job_id)
        if (
            job is None
            or job.status != PushJobStatus.processing.value
            or job.locked_by != worker.worker_id
            or not needs_check(job)
            or await _has_deliveries(session, job_id)
        ):
            return None
        assignment_id = _assignment_id(job)
        if assignment_id is None:
            return _gone("no_assignment_id")
        settled, due_epoch = await _rows_verdict(session, job, assignment_id, now)
        if settled is not None:
            return settled
        fetcher = worker.fetcher
        if breaker.opened_by is not None:
            return _unverified("breaker_open")
        if worker.cipher is None or fetcher is None:
            return _unverified("moodle_unconfigured")
        try:
            token = await load_moodle_token(
                session, worker.cipher, user_id=job.user_id
            )
        except CredentialInvalid as exc:
            return _unverified(exc.reason)
    return await _ask_moodle(
        fetcher,
        timeout=worker.settings.push_presend_timeout_seconds,
        token=token,
        assignment_id=assignment_id,
        due_epoch=due_epoch,
        breaker=breaker,
    )


async def recheck_rows(
    session: AsyncSession, job: PushJob, now: datetime
) -> PresendCheck | None:
    """The rows' verdict again, read under the job's row lock right before
    delivery, or None while the job is still due. The verdict that let the
    job through was read before the Moodle call, and a submission the probe
    or an app records during that call must not be sent past."""
    assignment_id = _assignment_id(job)
    if assignment_id is None:
        return _gone("no_assignment_id")
    settled, _ = await _rows_verdict(session, job, assignment_id, now)
    return settled


async def record_submitted(
    session: AsyncSession, *, user_id: uuid.UUID, check: PresendCheck, now: datetime
) -> None:
    """Write a `submitted` verdict back as the submission probe writes what
    it finds, in the transaction that cancels the job: the user's rows of
    the assignment marked submitted, its pending reminders cancelled, its
    running countdowns ended, and a sync asked of the user's devices when a
    row changed. The cancelling does not wait on a row changing here: one an
    app's upload marked submitted cancelled nothing."""
    assignment_id = check.moodle_assignment_id
    if assignment_id is None:
        return
    changed = await apply_submission_status(
        session,
        user_id=user_id,
        fetched={
            assignment_id: FetchedSubmission(
                moodle_assignment_id=assignment_id,
                is_submitted=True,
                submitted_at=check.submitted_at,
            )
        },
        now=now,
    )
    await cancel_for_submitted(
        session, user_id=user_id, moodle_assignment_ids={assignment_id}, now=now
    )
    if changed:
        await enqueue_sync_trigger(session, user_id, source="presend")


def _gone(reason: str) -> PresendCheck:
    return PresendCheck("gone", reason)


def _unverified(reason: str) -> PresendCheck:
    return PresendCheck("unverified", reason)


async def _has_deliveries(session: AsyncSession, job_id: int) -> bool:
    row = (
        await session.execute(
            select(PushDelivery.id).where(PushDelivery.push_job_id == job_id).limit(1)
        )
    ).first()
    return row is not None


def _assignment_id(job: PushJob) -> int | None:
    """The Moodle assignment `job` is about. A reminder names it; a
    countdown's source id is the id as text (see
    `submission_cancel._activity_id_for_assignment`)."""
    payload = job.payload or {}
    if job.channel == REMINDER_CHANNEL:
        value = payload.get("moodle_assignment_id")
        return value if type(value) is int else None
    value = payload.get("source_id")
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


async def _assignment_rows(
    session: AsyncSession, user_id: uuid.UUID, assignment_id: int
) -> list[Row[tuple[UserAssignment, str | None]]]:
    """Every row of the assignment the user has, with its mark."""
    return list(
        (
            await session.execute(
                select(UserAssignment, UserAssignmentOverride.local_status)
                .outerjoin(
                    UserAssignmentOverride,
                    UserAssignmentOverride.user_assignment_id == UserAssignment.id,
                )
                .where(
                    UserAssignment.user_id == user_id,
                    UserAssignment.moodle_assignment_id == assignment_id,
                )
            )
        ).all()
    )


async def _rows_verdict(
    session: AsyncSession, job: PushJob, assignment_id: int, now: datetime
) -> tuple[PresendCheck | None, int | None]:
    """What the user's rows of the assignment settle, or None to ask Moodle,
    and when the job's assignment is due (`_due_epoch`)."""
    rows = await _assignment_rows(session, job.user_id, assignment_id)
    due_epoch = _due_epoch(job, [row for row, _ in rows if row.deleted_at is None])
    verdict = await _database_verdict(session, job, assignment_id, rows, due_epoch, now)
    return verdict, due_epoch


async def _database_verdict(
    session: AsyncSession,
    job: PushJob,
    assignment_id: int,
    rows: list[Row[tuple[UserAssignment, str | None]]],
    due_epoch: int | None,
    now: datetime,
) -> PresendCheck | None:
    """What the user's rows settle, or None to ask Moodle.

    A submission, a mark and an ignored course count on any row, whatever
    its course id and deleted state. An assignment has one row, but a
    soft-deleted upload placeholder (course id 0) that the placeholder merge
    (migration d899fe7d1f79) left beside the server's row keeps its mark,
    which the apps no longer show, and that mark still holds a reminder back.
    """
    assignments = [row for row, _ in rows]
    submitted = next((row for row in assignments if row.provider_is_submitted), None)
    if submitted is not None:
        return PresendCheck(
            "submitted", "database", assignment_id, submitted.provider_submitted_at
        )
    if any(status in EXCLUDED_LOCAL_STATUSES for _, status in rows):
        return _gone("marked")
    if assignments:
        ignored = await ignored_courses(session, {job.user_id}, now)
        if any(in_ignored_course(row, ignored) for row in assignments):
            return _gone("course_ignored")
    alive = [row for row in assignments if row.deleted_at is None]
    if not alive:
        return _gone("no_row")
    if job.channel == REMINDER_CHANNEL and not any(
        row.due_at is not None and int(row.due_at.timestamp()) == due_epoch
        for row in alive
    ):
        # The scan files the reminder for the new due time.
        return _gone("due_changed")
    if due_epoch is not None and due_epoch <= now.timestamp():
        # Deferred past it, or claimed late: "due in 5 minutes" would be
        # wrong now.
        return _gone("due_passed")
    return None


def _due_epoch(job: PushJob, alive: list[UserAssignment]) -> int | None:
    """When the assignment is due, in unix seconds: the due time a reminder
    was filed for, or the time a countdown counts down to as `/schedule/sync`
    stored it, else the latest due time among the assignment's rows."""
    payload = job.payload or {}
    if job.channel == REMINDER_CHANNEL:
        due_epoch = payload.get("due_epoch")
        return int(due_epoch) if isinstance(due_epoch, (int, float)) else None
    target = _to_unix_seconds(payload.get("countdownTarget"))
    if target is not None:
        return target
    return max(
        (int(row.due_at.timestamp()) for row in alive if row.due_at is not None),
        default=None,
    )


async def _ask_moodle(
    fetcher: SubmissionStatusChecker,
    *,
    timeout: float,
    token: str,
    assignment_id: int,
    due_epoch: int | None,
    breaker: MoodleBreaker,
) -> PresendCheck:
    try:
        # The whole call's budget, and each request's timeout as well, so
        # it holds whatever moodle_fetch_timeout_seconds says.
        async with asyncio.timeout(timeout):
            answer = await fetcher.check_submission_status(
                token=token, assignment_id=assignment_id, timeout_seconds=timeout
            )
    except TimeoutError:
        return _open(breaker, "timeout")
    except MoodleUnavailable:
        return _open(breaker, "unavailable")
    except MoodleRateLimited:
        return _open(breaker, "rate_limited")
    except MoodleTokenInvalid:
        return _unverified("token_invalid")
    except MoodleRefused:
        # Moodle's answer about this token or assignment, such as
        # wsaccessusersuspended or accessexception: the others are asked.
        return _unverified("refused")
    except Exception as exc:
        # This frame holds the token, and exc_info renders every message in
        # the chain, so the chain is scrubbed first, as the probe's own
        # handlers do.
        logger.error(
            "push.presend_check_failed",
            error=type(exc).__name__,
            exc_info=_without_token(exc, token),
        )
        return _unverified("error")
    if answer is None:
        return _unverified("no_answer")
    if answer.is_submitted:
        return PresendCheck("submitted", "moodle", assignment_id, answer.submitted_at)
    if not answer.submissions_enabled:
        # Nothing to hand in online; both apps leave such an assignment out.
        return _gone("submissions_disabled")
    if answer.locked:
        return _gone("locked")
    if answer.extension_due_at is not None and (
        due_epoch is None or answer.extension_due_at.timestamp() > due_epoch
    ):
        # The job counts down to a deadline this student no longer has.
        return _gone("extension")
    return PresendCheck("send", "moodle", assignment_id)


def _open(breaker: MoodleBreaker, reason: str) -> PresendCheck:
    breaker.opened_by = reason
    return _unverified(reason)
