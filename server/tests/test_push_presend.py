"""The pre-send check: an assignment reminder or countdown start goes out
only once its assignment is confirmed still open, and never on a guess.

Moodle is a stub throughout (`SubmissionStatusStub` in conftest.py); the
token it is asked with is stored and read back through the real credential
path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from prometheus_client import REGISTRY
from sqlalchemy import select, update
from structlog.testing import capture_logs

from server import metrics
from server.auth.crypto import CredentialCipher
from server.auth.models import (
    DevicePushToken,
    ExternalAccount,
    ExternalAccountCredential,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory
from server.push import pipeline as pipeline_module
from server.push import presend as presend_module
from server.push.apns_client import SendResult
from server.push.dedupe import activity_end_key, schedule_key
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key
from server.push.router import PushRouter
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
)
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    FetchedSubmission,
    HttpAssignmentFetcher,
    MoodleRateLimited,
    MoodleRefused,
    MoodleTokenInvalid,
)
from server.tests.conftest import SubmissionStatusStub

pytestmark = pytest.mark.asyncio(loop_scope="session")

OPEN = {"is_submitted": False, "submitted_at": None}
SUBMITTED_AT = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)


class ScriptedSender:
    """PushSender double that always succeeds. Duplicated rather than
    shared, as in every push test module here."""

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


class HangingMoodle:
    """Moodle that never answers."""

    def __init__(self):
        self.asked = []

    async def check_submission_status(self, *, token, assignment_id, timeout_seconds):
        self.asked.append(assignment_id)
        await asyncio.sleep(30)


class MoodleWhile:
    """Moodle whose answer comes back once `meanwhile` has run: something
    elsewhere changes the database while the call is out."""

    def __init__(self, meanwhile, answer=OPEN):
        self.meanwhile = meanwhile
        self.answer = answer
        self.asked = []

    async def check_submission_status(self, *, token, assignment_id, timeout_seconds):
        self.asked.append(assignment_id)
        await self.meanwhile()
        return FetchedSubmission(moodle_assignment_id=assignment_id, **self.answer)


def _worker(prepared_engine, test_settings, moodle, *, apple=None, **settings):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings.model_copy(update=settings),
        router=PushRouter(apple=apple or ScriptedSender(), android=ScriptedSender()),
        worker_id="presend-test-worker",
        cipher=CredentialCipher.from_settings(test_settings),
        fetcher=moodle,
    )


async def _iphone_user(session, name="b11203058"):
    """A user whose iPhone takes both reminders and countdown starts."""
    user = User(student_id=name)
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id,
        client_device_id=f"iphone-{name}",
        platform="ios",
        app_version="2.1.0",
        cloud_sync_enabled=True,
        sync_assignment_reminders=True,
    )
    session.add(device)
    await session.flush()
    for kind in ("standard", "push_to_start"):
        session.add(
            DevicePushToken(
                device_id=device.id,
                provider="apns",
                token_kind=kind,
                token_hash=f"hash-{kind}-{name}",
                token_value=f"{kind}-{name}",
                bundle_id="org.ntust.app.TigerDuck",
            )
        )
    await session.flush()
    return user, device


def _tomorrow():
    return datetime.now(UTC) + timedelta(days=1)


def _ago(minutes):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _reminder(user, assignment_id, due_at, *, offset_hours=24.0, fire_at=None):
    """A reminder as the scan files it, due to fire unless `fire_at` says
    otherwise."""
    due_epoch = int(due_at.timestamp())
    fire_at = fire_at or _ago(5)
    return PushJob(
        user_id=user.id,
        dedupe_key=_dedupe_key(assignment_id, offset_hours, due_epoch),
        channel=REMINDER_CHANNEL,
        scenario=f"reminder_{offset_hours:g}h",
        fire_at=fire_at,
        available_at=fire_at,
        payload={
            "kind": "assignment_reminder",
            "assignment_title": f"HW{assignment_id}",
            "course_name": "資料結構",
            "moodle_assignment_id": assignment_id,
            "moodle_course_id": 7001,
            "due_at": due_at.isoformat(),
            "due_epoch": due_epoch,
            "offset_hours": offset_hours,
        },
    )


def _countdown_start(user, device, assignment_id, due_at):
    source_id = str(assignment_id)
    return PushJob(
        user_id=user.id,
        device_id=device.id,
        dedupe_key=schedule_key(device.id, source_id, "assignmentUrgent"),
        channel="schedule",
        scenario="assignmentUrgent",
        priority=10,
        fire_at=_ago(5),
        available_at=_ago(5),
        payload={
            "kind": "schedule",
            "scenario": "assignmentUrgent",
            "source_id": source_id,
            "activity_id": f"assignmentUrgent::{source_id}",
            "title": f"HW{assignment_id}",
            "subtitle": "資料結構",
            "accentHex": 1,
            "sourceId": source_id,
            "countdownTarget": due_at.isoformat(),
        },
    )


async def _open_assignment(
    make_assignment, store_moodle_token, user, aid, token="moodle-token"
):
    """An assignment due tomorrow, and the token to ask Moodle about it."""
    due_at = _tomorrow()
    row = await make_assignment(
        user_id=user.id, moodle_assignment_id=aid, due_at=due_at
    )
    await store_moodle_token(user.id, token=token)
    return row, due_at


async def _deliveries(session, job):
    return (
        (
            await session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )


async def _reload(session, row):
    await session.refresh(row)
    return row


def _reminders_and_starts(apple):
    """What a sender was handed, leaving out the sync trigger a flip files:
    it is claimed and sent in the same tick."""
    return [r for r in apple.requests if r.message.get("kind") != "sync_trigger"]


def _counted(verdict):
    return REGISTRY.get_sample_value(
        "tigerduck_push_presend_checks_total", {"verdict": verdict}
    )


async def _sync_trigger_keys(session, user):
    return (
        (
            await session.execute(
                select(PushJob.dedupe_key).where(
                    PushJob.user_id == user.id, PushJob.scenario == "sync_trigger"
                )
            )
        )
        .scalars()
        .all()
    )


# --- Moodle's word ----------------------------------------------------------


async def test_an_open_assignment_is_reminded(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 11
    )
    job = _reminder(user, 11, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    assert (await _reload(db_session, job)).status == "sent"
    assert moodle.asked == [11]
    assert moodle.tokens == ["moodle-token"]
    assert [r.device_token for r in apple.requests] == ["standard-b11203058"]


async def test_a_reminder_is_held_by_apns_until_its_due_time_at_most(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The copy written for each recipient keeps `due_epoch`, so a phone
    offline until after the deadline never shows the reminder."""
    user, _ = await _iphone_user(db_session)
    due_at = datetime.now(UTC) + timedelta(hours=2)
    await make_assignment(user_id=user.id, moodle_assignment_id=15, due_at=due_at)
    await store_moodle_token(user.id)
    db_session.add(_reminder(user, 15, due_at, offset_hours=2.0))
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(OPEN), apple=apple)
    )

    assert [r.expiration for r in apple.requests] == [int(due_at.timestamp())]


async def test_a_submission_moodle_reports_cancels_the_reminder_and_its_siblings(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 12
    )
    job = _reminder(user, 12, due_at)
    later = _reminder(
        user, 12, due_at, offset_hours=2.0, fire_at=due_at - timedelta(hours=2)
    )
    db_session.add_all([job, later])
    await db_session.commit()

    moodle = SubmissionStatusStub({"is_submitted": True, "submitted_at": SUBMITTED_AT})
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "submitted_before_send")
    assert await _deliveries(db_session, job) == []
    assert _reminders_and_starts(apple) == []
    assert (await _reload(db_session, later)).status == "cancelled"
    row = await _reload(db_session, row)
    assert (row.provider_is_submitted, row.provider_submitted_at) == (
        True,
        SUBMITTED_AT,
    )
    # The user's other devices hear of the flip on a trigger of its own.
    assert any(
        key.endswith(":presend") for key in await _sync_trigger_keys(db_session, user)
    )


@pytest.mark.parametrize(
    "answer,reason",
    [
        ({**OPEN, "submissions_enabled": False}, "submissions_disabled"),
        ({**OPEN, "locked": True}, "locked"),
        (
            {**OPEN, "extension_due_at": datetime.now(UTC) + timedelta(days=3)},
            "extension",
        ),
    ],
    ids=["no-online-submission", "locked", "extension"],
)
async def test_a_reminder_the_student_cannot_act_on_is_cancelled(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    monkeypatch,
    answer,
    reason,
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 13
    )
    job = _reminder(user, 13, due_at)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender()
    # The module's logger may be cached with an earlier app's processors,
    # which capture_logs does not reach; a new one binds inside it.
    monkeypatch.setattr(
        pipeline_module, "logger", structlog.get_logger(pipeline_module.__name__)
    )
    worker = _worker(
        prepared_engine, test_settings, SubmissionStatusStub(answer), apple=apple
    )
    with capture_logs() as logs:
        await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert apple.requests == []
    # The ids and the verdict, never the title or a token.
    assert [log for log in logs if log["event"] == "push.presend_check"] == [
        {
            "event": "push.presend_check",
            "log_level": "info",
            "job_id": job.id,
            "channel": "assignment",
            "verdict": "gone",
            "reason": reason,
            "deferred": False,
        }
    ]


async def test_an_extension_before_the_due_time_still_reminds(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 14
    )
    job = _reminder(user, 14, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(
        {**OPEN, "extension_due_at": due_at - timedelta(hours=1)}
    )
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert (await _reload(db_session, job)).status == "sent"


async def _extended_assignment(make_assignment, store_moodle_token, user, aid):
    """An assignment whose due date is the student's extension, as the
    probe stores it."""
    extension = _tomorrow()
    row = await make_assignment(
        user_id=user.id, moodle_assignment_id=aid, due_at=extension
    )
    row.extension_due_at = extension
    await store_moodle_token(user.id)
    return extension


async def test_a_reminder_at_the_students_extension_is_sent(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    extension = await _extended_assignment(
        make_assignment, store_moodle_token, user, 17
    )
    job = _reminder(user, 17, extension)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub({**OPEN, "extension_due_at": extension})
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert (await _reload(db_session, job)).status == "sent"


@pytest.mark.parametrize(
    "now_extended_by", [None, timedelta(hours=-2)], ids=["withdrawn", "shortened"]
)
async def test_a_reminder_at_an_extension_moodle_no_longer_gives_is_cancelled(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    monkeypatch,
    now_extended_by,
):
    user, _ = await _iphone_user(db_session)
    extension = await _extended_assignment(
        make_assignment, store_moodle_token, user, 18
    )
    job = _reminder(user, 18, extension)
    db_session.add(job)
    await db_session.commit()

    answer = {
        **OPEN,
        "extension_due_at": (
            None if now_extended_by is None else extension + now_extended_by
        ),
    }
    monkeypatch.setattr(
        pipeline_module, "logger", structlog.get_logger(pipeline_module.__name__)
    )
    apple = ScriptedSender()
    worker = _worker(
        prepared_engine, test_settings, SubmissionStatusStub(answer), apple=apple
    )
    with capture_logs() as logs:
        await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert apple.requests == []
    assert [
        log["reason"] for log in logs if log["event"] == "push.presend_check"
    ] == ["extension_withdrawn"]


async def test_a_reminder_at_a_due_date_moved_to_the_extension_survives_its_withdrawal(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The teacher moves the due date to the student's extension, and Moodle
    then drops the extension: the date never changed, so the reminder goes
    out."""
    user, _ = await _iphone_user(db_session)
    extension = await _extended_assignment(
        make_assignment, store_moodle_token, user, 19
    )
    await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[
            FetchedAssignment(
                moodle_course_id=7001,
                moodle_assignment_id=19,
                course_name=None,
                title="Assignment 19",
                due_at=extension,
                cutoff_at=None,
                allow_from_at=None,
                moodle_url=None,
                intro_html=None,
            )
        ],
        now=datetime.now(UTC),
    )
    job = _reminder(user, 19, extension)
    db_session.add(job)
    await db_session.commit()

    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(OPEN))
    )

    assert (await _reload(db_session, job)).status == "sent"
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.moodle_assignment_id == 19)
        )
    ).scalar_one()
    assert row.extension_due_at is None


@pytest.mark.parametrize("meanwhile", ["probe-finds-it-submitted", "marked-done"])
async def test_what_the_rows_learn_during_the_moodle_call_is_not_sent_past(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    meanwhile,
):
    """The probe or an app records the submission, or the user marks the
    assignment done, while Moodle is being asked: the rows are read again
    under the job's lock before anything is sent."""
    user, _ = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 16
    )
    job = _reminder(user, 16, due_at)
    db_session.add(job)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    async def change_the_rows():
        async with factory() as session:
            if meanwhile == "probe-finds-it-submitted":
                await session.execute(
                    update(UserAssignment)
                    .where(UserAssignment.id == row.id)
                    .values(provider_is_submitted=True)
                )
            else:
                await _mark(session, row, "locally_completed")
            await session.commit()

    apple = ScriptedSender()
    moodle = MoodleWhile(change_the_rows)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == (
        "cancelled",
        "submitted_before_send"
        if meanwhile == "probe-finds-it-submitted"
        else "stale_before_send",
    )
    assert await _deliveries(db_session, job) == []
    assert _reminders_and_starts(apple) == []


@pytest.mark.parametrize("lost", ["cleared", "taken-by-another-worker"])
async def test_a_claim_lost_during_the_moodle_call_is_left_alone(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    lost,
):
    """The portal's /push-clear cancels the job, or another worker takes it,
    while Moodle is asked: the verdict is dropped, with no delivery and
    nothing written back."""
    user, _ = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 19
    )
    job = _reminder(user, 19, due_at)
    db_session.add(job)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    async def lose_the_claim():
        values = (
            {"status": "cancelled", "cancelled_at": datetime.now(UTC)}
            if lost == "cleared"
            else {"locked_by": "another-worker"}
        )
        async with factory() as session:
            await session.execute(
                update(PushJob).where(PushJob.id == job.id).values(**values)
            )
            await session.commit()

    moodle = MoodleWhile(
        lose_the_claim, {"is_submitted": True, "submitted_at": SUBMITTED_AT}
    )
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == (
        ("cancelled", None) if lost == "cleared" else ("processing", None)
    )
    assert await _deliveries(db_session, job) == []
    assert apple.requests == []
    assert (await _reload(db_session, row)).provider_is_submitted is False
    assert await _sync_trigger_keys(db_session, user) == []


async def test_no_transaction_is_open_while_moodle_is_asked(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The job and the credential the check read can be locked from
    elsewhere, without waiting, while the Moodle call is out."""
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(make_assignment, store_moodle_token, user, 20)
    account = (
        await db_session.execute(
            select(ExternalAccount).where(ExternalAccount.user_id == user.id)
        )
    ).scalar_one()
    job = _reminder(user, 20, due_at)
    db_session.add(job)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    locked = []

    async def lock_without_waiting():
        async with factory() as session:
            await session.execute(
                select(PushJob.id)
                .where(PushJob.id == job.id)
                .with_for_update(nowait=True)
            )
            await session.execute(
                select(ExternalAccountCredential.external_account_id)
                .where(ExternalAccountCredential.external_account_id == account.id)
                .with_for_update(nowait=True)
            )
            await session.rollback()
        locked.append(True)

    moodle = MoodleWhile(lock_without_waiting)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert locked == [True]
    assert (await _reload(db_session, job)).status == "sent"


# --- The database's word ----------------------------------------------------


async def test_a_submission_on_a_deleted_twin_settles_it_without_moodle(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """An app's upload lands under course id 0 on a row the server's sync
    soft-deleted; its "submitted" still counts, and marks the row the scan
    reads."""
    user, _ = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 21
    )
    await make_assignment(
        user_id=user.id,
        moodle_assignment_id=21,
        moodle_course_id=0,
        due_at=due_at,
        provider_is_submitted=True,
        deleted_at=_ago(60),
    )
    job = _reminder(user, 21, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "submitted_before_send")
    assert moodle.asked == []
    assert (await _reload(db_session, row)).provider_is_submitted is True
    assert await _deliveries(db_session, job) == []


async def _mark(session, row, status):
    session.add(
        UserAssignmentOverride(
            user_id=row.user_id, user_assignment_id=row.id, local_status=status
        )
    )


@pytest.mark.parametrize(
    "on_twin", [False, True], ids=["on-the-row", "on-a-deleted-twin"]
)
@pytest.mark.parametrize("status", ["locally_completed", "ignored", "archived"])
async def test_a_marked_assignment_is_not_reminded(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    status,
    on_twin,
):
    user, _ = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 22
    )
    if on_twin:
        row = await make_assignment(
            user_id=user.id,
            moodle_assignment_id=22,
            moodle_course_id=0,
            due_at=due_at,
            deleted_at=_ago(60),
        )
    await _mark(db_session, row, status)
    job = _reminder(user, 22, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert moodle.asked == []


async def test_an_assignment_in_an_ignored_course_is_not_reminded(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 23
    )
    course = UserCourse(
        user_id=user.id,
        semester="1141",
        course_key="client:1141:CS1001301",
        course_no="CS1001301",
        course_name="資料結構",
    )
    db_session.add(course)
    await db_session.flush()
    db_session.add(
        UserCourseOverride(
            user_id=user.id,
            user_course_id=course.id,
            moodle_course_id=7001,
            ignore_assignments=True,
        )
    )
    job = _reminder(user, 23, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert moodle.asked == []


@pytest.mark.parametrize("case", ["deleted", "due-moved", "due-passed", "never-synced"])
async def test_a_reminder_with_nothing_left_to_remind_of_is_cancelled(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    case,
):
    user, _ = await _iphone_user(db_session)
    await store_moodle_token(user.id)
    due_at = _tomorrow()
    if case == "deleted":
        await make_assignment(
            user_id=user.id, moodle_assignment_id=24, due_at=due_at, deleted_at=_ago(60)
        )
    elif case == "due-moved":
        await make_assignment(
            user_id=user.id,
            moodle_assignment_id=24,
            due_at=due_at + timedelta(days=2),
        )
    elif case == "due-passed":
        # Due a minute ago: "due in 5 minutes" would be wrong now.
        due_at = _ago(1)
        await make_assignment(user_id=user.id, moodle_assignment_id=24, due_at=due_at)
    job = _reminder(user, 24, due_at, offset_hours=5 / 60)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert moodle.asked == []


# --- No answer --------------------------------------------------------------


async def test_an_unanswered_check_holds_the_reminder_then_drops_it_after_the_grace(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 31
    )
    job = _reminder(user, 31, due_at)
    db_session.add(job)
    await db_session.commit()
    worker = _worker(prepared_engine, test_settings, SubmissionStatusStub(None))

    before = datetime.now(UTC)
    await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.attempts, job.locked_by) == ("pending", 0, None)
    assert job.available_at >= before + timedelta(
        seconds=test_settings.push_presend_retry_seconds
    )
    assert await _deliveries(db_session, job) == []

    # Still no answer once the fire time is past its grace.
    job.fire_at = _ago(test_settings.push_presend_grace_minutes + 1)
    job.available_at = _ago(1)
    await db_session.commit()
    await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "unverified_before_send")
    assert job.attempts == 0
    assert await _deliveries(db_session, job) == []


async def test_a_reminder_confirmed_only_after_its_grace_is_not_sent(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """Its copy counts from the fire time ("30 minutes left"). Claimed late,
    or confirmed open only after retries ran past the grace, it would be
    wrong by the time it arrived."""
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 18
    )
    job = _reminder(
        user, 18, due_at, fire_at=_ago(test_settings.push_presend_grace_minutes + 1)
    )
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(OPEN), apple=apple)
    )

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert apple.requests == []
    assert await _deliveries(db_session, job) == []


@pytest.mark.parametrize("credential", ["none", "invalid"])
async def test_without_a_usable_token_moodle_is_not_asked_and_nothing_is_sent(
    db_session, prepared_engine, test_settings, make_assignment, credential
):
    """No account, or one whose credential the sync worker found dead."""
    user, _ = await _iphone_user(db_session)
    due_at = _tomorrow()
    await make_assignment(user_id=user.id, moodle_assignment_id=32, due_at=due_at)
    job = _reminder(user, 32, due_at)
    db_session.add(job)
    await db_session.commit()
    if credential == "invalid":
        db_session.add(
            ExternalAccount(
                user_id=user.id,
                provider="ntust_sso",
                external_user_id="b11203058",
                credential_status="invalid",
            )
        )
        await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert (await _reload(db_session, job)).status == "pending"
    assert moodle.asked == []


async def test_a_server_without_credential_keys_sends_no_reminder(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 33
    )
    job = _reminder(user, 33, due_at)
    db_session.add(job)
    await db_session.commit()
    apple = ScriptedSender()
    worker = PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(apple=apple, android=ScriptedSender()),
        worker_id="presend-test-worker",
    )

    await run_push_tick(worker)

    assert (await _reload(db_session, job)).status == "pending"
    assert apple.requests == []


async def _three_due_reminders(session, make_assignment, store_moodle_token):
    """Three users' reminders, claimed in this order, each asked about with
    a token of its own (`tok-41`, ...)."""
    jobs = []
    for index, aid in enumerate((41, 42, 43)):
        user, _ = await _iphone_user(session, name=f"b1120305{index}")
        _, due_at = await _open_assignment(
            make_assignment, store_moodle_token, user, aid, token=f"tok-{aid}"
        )
        jobs.append(_reminder(user, aid, due_at, fire_at=_ago(3 - index)))
    session.add_all(jobs)
    await session.commit()
    return jobs


async def test_a_rate_limiting_moodle_is_asked_once_a_tick(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = SubmissionStatusStub(MoodleRateLimited("http_429"), OPEN)
    worker = _worker(prepared_engine, test_settings, moodle)

    await run_push_tick(worker)

    assert moodle.asked == [41]
    for job in jobs:
        assert (await _reload(db_session, job)).status == "pending"

    # The next tick asks again, and Moodle answers.
    for job in jobs:
        job.available_at = _ago(1)
    await db_session.commit()
    await run_push_tick(worker)

    # Once Moodle has answered, the rest of the batch is asked side by side.
    assert (moodle.asked[:2], sorted(moodle.asked[2:])) == ([41, 41], [42, 43])
    for job in jobs:
        assert (await _reload(db_session, job)).status == "sent"


async def test_a_moodle_that_hangs_costs_one_timeout_a_tick(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = HangingMoodle()

    await run_push_tick(
        _worker(
            prepared_engine, test_settings, moodle, push_presend_timeout_seconds=0.05
        )
    )

    assert moodle.asked == [41]
    for job in jobs:
        assert (await _reload(db_session, job)).status == "pending"


class CountingMoodle:
    """Moodle answering every assignment open, counting the calls it has out
    at once. After the first, a call waits up to a second for company."""

    def __init__(self):
        self.calls = 0
        self.out = 0
        self.most_out = 0

    async def check_submission_status(self, *, token, assignment_id, timeout_seconds):
        self.calls += 1
        self.out += 1
        self.most_out = max(self.most_out, self.out)
        for _ in range(100 if self.calls > 1 else 0):
            if self.out > 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        self.out -= 1
        return FetchedSubmission(moodle_assignment_id=assignment_id, **OPEN)


async def test_a_batchs_checks_run_side_by_side_once_moodle_answers(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """One at a time, a deadline rush's checks held every job behind them for
    minutes when Moodle slowed down (#98). The first goes alone, so an
    outage still costs one call a tick."""
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = CountingMoodle()

    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert moodle.most_out == 2
    for job in jobs:
        assert (await _reload(db_session, job)).status == "sent"


async def test_a_job_no_check_guards_opens_no_check(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token,
    monkeypatch,
):
    """Every claimed job opened a transaction for the check before the check
    turned it away (#98)."""
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(make_assignment, store_moodle_token, user, 44)
    reminder = _reminder(user, 44, due_at)
    other = PushJob(
        user_id=user.id,
        dedupe_key="system:test:other",
        channel="system",
        scenario="reauth_required",
        fire_at=_ago(5),
        available_at=_ago(5),
        payload={"title": "t", "body": "b"},
    )
    db_session.add_all([reminder, other])
    await db_session.commit()
    real_check = pipeline_module.check_before_send
    checked = []

    async def check(worker, *, job_id, breaker):
        checked.append(job_id)
        return await real_check(worker, job_id=job_id, breaker=breaker)

    monkeypatch.setattr(pipeline_module, "check_before_send", check)
    await run_push_tick(_worker(prepared_engine, test_settings, SubmissionStatusStub()))

    assert checked == [reminder.id]


def _moodle_answering(handler):
    """The real fetcher, talking to `handler` instead of Moodle."""
    return HttpAssignmentFetcher(
        base_url="https://moodle.example.test",
        timeout_seconds=20.0,
        transport=httpx.MockTransport(handler),
    )


_SITE_INFO = {"userid": 42, "sitename": "test"}
_OPEN_ANSWER = {
    "lastattempt": {
        "submissionsenabled": True,
        "locked": False,
        "extensionduedate": None,
        "gradingstatus": "notgraded",
    },
    "warnings": [],
}


@pytest.mark.parametrize(
    "failure", ["connection-refused", "read-timeout", "http-503", "html-page"]
)
async def test_moodle_failing_for_everyone_opens_the_breaker(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    failure,
):
    """No connection, a timeout of the request itself (whatever the order
    of the two timeout settings), a 5xx or a page that is not JSON say
    Moodle is failing as a whole: the first job's check opens the breaker,
    and the tick's other jobs are not asked."""
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    tokens = []

    def handler(request):
        tokens.append(request.url.params["wstoken"])
        if failure == "connection-refused":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "read-timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "http-503":
            return httpx.Response(503, text="<html>Service Unavailable</html>")
        return httpx.Response(200, text="<html>Moodle is under maintenance</html>")

    await run_push_tick(
        _worker(prepared_engine, test_settings, _moodle_answering(handler))
    )

    assert tokens == ["tok-41"]
    for job in jobs:
        assert (await _reload(db_session, job)).status == "pending"


@pytest.mark.parametrize("refused", ["site-info", "submission-status"])
async def test_moodle_refusing_one_account_holds_back_only_its_job(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    refused,
):
    """A Moodle exception in a JSON answer is about one token or one
    assignment; the other jobs are still asked, and sent."""
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    tokens = []

    def handler(request):
        token = request.url.params["wstoken"]
        site_info = request.url.params["wsfunction"] == "core_webservice_get_site_info"
        tokens.append(token)
        if token == "tok-41" and site_info == (refused == "site-info"):
            return httpx.Response(
                200,
                json={
                    "exception": "moodle_exception",
                    "errorcode": (
                        "wsaccessusersuspended" if site_info else "invalidrecord"
                    ),
                    "message": "refused",
                },
            )
        return httpx.Response(200, json=_SITE_INFO if site_info else _OPEN_ANSWER)

    await run_push_tick(
        _worker(prepared_engine, test_settings, _moodle_answering(handler))
    )

    assert {"tok-42", "tok-43"} <= set(tokens)
    assert [(await _reload(db_session, job)).status for job in jobs] == [
        "pending",
        "sent",
        "sent",
    ]


async def test_one_users_dead_token_does_not_hold_back_the_others(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = SubmissionStatusStub(MoodleTokenInvalid("invalidtoken"), OPEN)

    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    # The first goes alone; once Moodle has answered, the rest side by side.
    assert (moodle.asked[0], sorted(moodle.asked)) == (41, [41, 42, 43])
    assert [(await _reload(db_session, job)).status for job in jobs] == [
        "pending",
        "sent",
        "sent",
    ]


@pytest.mark.parametrize("errorcode", ["wsaccessusersuspended", "accessexception"])
async def test_one_users_refusal_does_not_hold_back_the_others(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    errorcode,
):
    """Moodle refuses a suspended student's or an expired token's site-info
    request at once (`MoodleRefused`). That is the one account's answer, not
    Moodle down for everyone."""
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = SubmissionStatusStub(MoodleRefused(errorcode), OPEN)

    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert (moodle.asked[0], sorted(moodle.asked)) == (41, [41, 42, 43])
    assert [(await _reload(db_session, job)).status for job in jobs] == [
        "pending",
        "sent",
        "sent",
    ]


async def test_an_unexpected_error_is_logged_without_the_token(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    monkeypatch,
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 44
    )
    job = _reminder(user, 44, due_at)
    db_session.add(job)
    await db_session.commit()
    leaky = RuntimeError("refused wstoken=moodle-token&wsfunction=x")
    monkeypatch.setattr(
        presend_module, "logger", structlog.get_logger(presend_module.__name__)
    )
    with capture_logs() as logs:
        await run_push_tick(
            _worker(prepared_engine, test_settings, SubmissionStatusStub(leaky))
        )

    assert (await _reload(db_session, job)).status == "pending"
    failed = [log for log in logs if log["event"] == "push.presend_check_failed"]
    assert [log["error"] for log in failed] == ["RuntimeError"]
    assert "moodle-token" not in str(failed[0]["exc_info"])


# --- Which jobs are checked -------------------------------------------------


async def test_a_countdown_start_for_a_submitted_assignment_is_cancelled(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, device = await _iphone_user(db_session)
    row, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 51
    )
    job = _countdown_start(user, device, 51, due_at)
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub({"is_submitted": True, "submitted_at": None})
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "submitted_before_send")
    assert moodle.asked == [51]
    assert _reminders_and_starts(apple) == []
    assert (await _reload(db_session, row)).provider_is_submitted is True


async def test_a_countdown_start_for_an_open_assignment_goes_out(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, device = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 52
    )
    job = _countdown_start(user, device, 52, due_at)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(OPEN), apple=apple)
    )

    assert (await _reload(db_session, job)).status == "sent"
    assert [r.device_token for r in apple.requests] == ["push_to_start-b11203058"]


async def test_a_countdown_start_moodle_cannot_confirm_is_held(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, device = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 58
    )
    job = _countdown_start(user, device, 58, due_at)
    db_session.add(job)
    await db_session.commit()

    before = datetime.now(UTC)
    apple = ScriptedSender()
    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(None), apple=apple)
    )

    job = await _reload(db_session, job)
    assert (job.status, job.attempts, job.locked_by) == ("pending", 0, None)
    assert job.available_at >= before + timedelta(
        seconds=test_settings.push_presend_retry_seconds
    )
    assert apple.requests == []
    assert await _deliveries(db_session, job) == []


async def test_a_countdown_start_past_its_countdown_is_cancelled(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The due time of a start is the `countdownTarget` it counts down to,
    even when the row says later."""
    user, device = await _iphone_user(db_session)
    await _open_assignment(make_assignment, store_moodle_token, user, 57)
    job = _countdown_start(user, device, 57, _ago(1))
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert moodle.asked == []


async def test_a_countdown_start_to_a_moved_deadline_is_cancelled(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """A start filed before the deadline moved would count down to the old
    one; the device's next sync files it again for the new one."""
    user, device = await _iphone_user(db_session)
    _, due_at = await _open_assignment(make_assignment, store_moodle_token, user, 59)
    job = _countdown_start(user, device, 59, due_at - timedelta(hours=2))
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    assert moodle.asked == []
    assert _reminders_and_starts(apple) == []


async def test_a_live_activity_end_is_not_checked(
    db_session, prepared_engine, test_settings
):
    """An end takes down a countdown already on screen, so it goes whatever
    the assignment's state, and with no row or token to check against."""
    user, device = await _iphone_user(db_session)
    activity_id = "assignmentUrgent::53"
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash="hash-la-53",
            token_value="la-53",
            bundle_id="org.ntust.app.TigerDuck",
            scope_key=activity_id,
        )
    )
    job = PushJob(
        user_id=user.id,
        device_id=device.id,
        dedupe_key=activity_end_key(device.id, activity_id),
        channel="schedule",
        scenario="activityEnd",
        priority=10,
        fire_at=_ago(5),
        available_at=_ago(5),
        payload={
            "kind": "live_activity_end",
            "activity_id": activity_id,
            "source_id": "53",
        },
    )
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, moodle, apple=apple))

    assert (await _reload(db_session, job)).status == "sent"
    assert moodle.asked == []
    assert [r.device_token for r in apple.requests] == ["la-53"]


async def test_a_retry_round_is_not_checked_again(
    db_session, prepared_engine, test_settings
):
    """Once a delivery row exists the reminder may already have reached a
    device, so a later round finishes it whatever the assignment says now."""
    user, device = await _iphone_user(db_session)
    job = _reminder(user, 54, _tomorrow())
    job.attempts = 1
    db_session.add(job)
    await db_session.flush()
    token = (
        await db_session.execute(
            select(DevicePushToken).where(
                DevicePushToken.device_id == device.id,
                DevicePushToken.token_kind == "standard",
            )
        )
    ).scalar_one()
    db_session.add(
        PushDelivery(
            push_job_id=job.id,
            user_id=user.id,
            device_id=device.id,
            push_token_id=token.id,
            provider="apns",
            token_kind="standard",
            token_hash=token.token_hash,
            next_retry_at=_ago(1),
        )
    )
    await db_session.commit()

    moodle = SubmissionStatusStub(OPEN)
    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert (await _reload(db_session, job)).status == "sent"
    assert moodle.asked == []


async def test_a_job_the_check_missed_is_put_back_for_the_next_round(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    monkeypatch,
):
    """Ticks share one worker id. One that recovers this job as stale and
    claims it again while this tick's check runs shows the check a job not
    claimed and the lock afterwards one that is: it must not go out
    unchecked."""
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 17
    )
    job = _reminder(user, 17, due_at)
    db_session.add(job)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    real_check = pipeline_module.check_before_send

    async def set_claim(job_id, **values):
        async with factory() as session:
            await session.execute(
                update(PushJob).where(PushJob.id == job_id).values(**values)
            )
            await session.commit()

    async def check_while_another_tick_reclaims(worker, *, job_id, breaker):
        await set_claim(job_id, status="pending", locked_by=None, locked_at=None)
        result = await real_check(worker, job_id=job_id, breaker=breaker)
        await set_claim(
            job_id,
            status="processing",
            locked_by=worker.worker_id,
            locked_at=datetime.now(UTC),
        )
        return result

    monkeypatch.setattr(
        pipeline_module, "check_before_send", check_while_another_tick_reclaims
    )
    moodle = SubmissionStatusStub({"is_submitted": True, "submitted_at": None})
    apple = ScriptedSender()
    worker = _worker(prepared_engine, test_settings, moodle, apple=apple)

    await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.locked_by, job.attempts) == ("pending", None, 0)
    assert apple.requests == []
    assert await _deliveries(db_session, job) == []

    # The next round checks it.
    monkeypatch.setattr(pipeline_module, "check_before_send", real_check)
    await run_push_tick(worker)

    job = await _reload(db_session, job)
    assert (job.status, job.last_error) == ("cancelled", "submitted_before_send")
    assert await _deliveries(db_session, job) == []


async def test_each_verdict_is_counted(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user, _ = await _iphone_user(db_session)
    _, due_at = await _open_assignment(
        make_assignment, store_moodle_token, user, 55
    )
    db_session.add(_reminder(user, 55, due_at))
    db_session.add(_reminder(user, 56, due_at, fire_at=_ago(4)))
    await db_session.commit()
    before = {verdict: _counted(verdict) for verdict in metrics.PRESEND_VERDICTS}

    await run_push_tick(
        _worker(prepared_engine, test_settings, SubmissionStatusStub(OPEN))
    )

    after = {verdict: _counted(verdict) for verdict in metrics.PRESEND_VERDICTS}
    assert {v: after[v] - before[v] for v in after} == {
        "send": 1,
        "submitted": 0,
        "gone": 1,
        "unverified": 0,
    }
