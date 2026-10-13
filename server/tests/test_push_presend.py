"""The pre-send check: an assignment reminder or countdown start goes out
only once its assignment is confirmed still open, and never on a guess.

Moodle is a stub throughout (`SubmissionStatusStub` in conftest.py); the
token it is asked with is stored and read back through the real credential
path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from prometheus_client import REGISTRY
from sqlalchemy import select
from structlog.testing import capture_logs

from server import metrics
from server.auth.crypto import CredentialCipher
from server.auth.models import (
    DevicePushToken,
    ExternalAccount,
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
from server.sync.models import UserAssignmentOverride, UserCourse, UserCourseOverride
from server.syncjobs.moodle_client import (
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
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

    async def fetch_submission_status(self, *, token, assignment_ids, max_concurrency):
        self.asked.extend(assignment_ids)
        await asyncio.sleep(30)
        return {}


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


async def _open_assignment(make_assignment, store_moodle_token, user, aid):
    """An assignment due tomorrow, and the token to ask Moodle about it."""
    due_at = _tomorrow()
    row = await make_assignment(
        user_id=user.id, moodle_assignment_id=aid, due_at=due_at
    )
    await store_moodle_token(user.id)
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
    """Three users' reminders, claimed in this order."""
    jobs = []
    for index, aid in enumerate((41, 42, 43)):
        user, _ = await _iphone_user(session, name=f"b1120305{index}")
        _, due_at = await _open_assignment(
            make_assignment, store_moodle_token, user, aid
        )
        jobs.append(_reminder(user, aid, due_at, fire_at=_ago(3 - index)))
    session.add_all(jobs)
    await session.commit()
    return jobs


@pytest.mark.parametrize(
    "failure",
    [MoodleUnreachable("submission_probe_unreachable"), MoodleRateLimited("http_429")],
    ids=["unreachable", "rate-limited"],
)
async def test_moodle_failing_for_everyone_is_asked_once_a_tick(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    failure,
):
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = SubmissionStatusStub(failure, OPEN)
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

    assert moodle.asked == [41, 41, 42, 43]
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


async def test_one_users_dead_token_does_not_hold_back_the_others(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    jobs = await _three_due_reminders(db_session, make_assignment, store_moodle_token)
    moodle = SubmissionStatusStub(MoodleTokenInvalid("invalidtoken"), OPEN)

    await run_push_tick(_worker(prepared_engine, test_settings, moodle))

    assert moodle.asked == [41, 42, 43]
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
