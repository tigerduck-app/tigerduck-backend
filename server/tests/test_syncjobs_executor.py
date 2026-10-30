"""Executor: stale-lock recovery, claiming, global cap, execution paths."""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from sqlalchemy import func, select, text, update

from server.academic_calendar.models import SemesterTerm
from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    DevicePushToken,
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
    UserDevice,
)
from server.config import Settings
from server.db import build_session_factory
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key, scan_assignment_reminders
from server.logging_setup import configure
from server.auth import service as auth_service
from server.sync.models import (
    UserAssignment,
    UserChangeLog,
    UserCourse,
    UserSyncState,
)
from server.syncjobs import executor as executor_module
from server.syncjobs.executor import SyncWorker, run_sync_tick
from server.syncjobs.models import SyncJob, SyncJobType, SyncLogEntry, SyncPolicy, SyncRun
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    FetchedCourse,
    FetchedSubmission,
    HttpAssignmentFetcher,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoUnavailable,
)
from server.syncjobs.policies import DEFAULT_POLICIES, ensure_default_policies

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cipher() -> CredentialCipher:
    return CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )


class StubFetcher:
    def __init__(self, results=None, errors=()):
        self.results = results if results is not None else []
        self.errors = list(errors)
        self.calls: list[str] = []

    async def fetch_assignments(self, *, token):
        self.calls.append(token)
        if self.errors:
            raise self.errors.pop(0)
        return self.results


class StubCourseFetcher:
    def __init__(self, results=None, errors=(), fullname=None):
        self.results = results if results is not None else []
        self.errors = list(errors)
        self.fullname = fullname

    async def fetch_courses(self, *, token, on_fullname=None):
        if self.errors:
            raise self.errors.pop(0)
        if on_fullname is not None and self.fullname is not None:
            on_fullname(self.fullname)
        return self.results


async def _publish_term(session, code="1151"):
    """A term in progress: the scan reminds of its courses only."""
    today = datetime.now(UTC).date()
    session.add(
        SemesterTerm(
            code=code,
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=90),
        )
    )
    await session.flush()


def _worker(
    prepared_engine, test_settings, fetcher=None, course_fetcher=None
) -> SyncWorker:
    # Neutralise the Moodle maintenance window (defaults to 00:00–05:00
    # Taipei): during the window _handle_moodle_failure reschedules instead
    # of disabling/backing off, which would make the failure-path tests
    # depend on wall-clock time. start == end means "never in window".
    settings = test_settings.model_copy(
        update={
            "moodle_maintenance_start": "00:00",
            "moodle_maintenance_end": "00:00",
        }
    )
    return SyncWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=settings,
        cipher=_cipher(),
        fetcher=fetcher if fetcher is not None else StubFetcher(),
        course_fetcher=course_fetcher if course_fetcher is not None else StubCourseFetcher(),
        worker_id="test-worker",
    )


async def _setup_user_job(
    session,
    *,
    student_id="b11203058",
    token="tok-1",
    password_verified=True,
    job_kwargs=None,
    syncing_device=True,
):
    cipher = _cipher()
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    if syncing_device:
        # A Mac: it keeps the job claimable and receives neither reminders
        # nor the reauth push, so no test sees a push it did not set up.
        session.add(
            UserDevice(
                user_id=user.id,
                client_device_id=f"mac-{student_id}",
                platform="macos",
                last_seen_at=datetime.now(UTC),
            )
        )
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=student_id
    )
    session.add(account)
    await session.flush()
    blob = cipher.encrypt(
        {
            "ntust_password": "pw",
            "password_verified": password_verified,
            "token_cache": {"moodle_token": token},
        },
        build_credential_aad(account.id, "ntust_sso"),
    )
    session.add(
        ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id=blob.key_id,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            aad=blob.aad,
        )
    )
    # Already due by the clock the executor claims with: this host's. Left
    # to its server default, `run_after` would be the database clock's
    # `now()`, and a database clock running even a few milliseconds ahead of
    # this host's leaves the job not yet due when the tick claims (pinned by
    # `test_a_job_set_up_here_is_due_whichever_way_the_clocks_disagree`).
    fields = {
        "run_after": datetime.now(UTC) - timedelta(minutes=1),
        **(job_kwargs or {}),
    }
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
        **fields,
    )
    session.add(job)
    await ensure_default_policies(session)
    await session.commit()
    return user, account, job


def _fa(aid: int, *, due_at: datetime | None = None) -> FetchedAssignment:
    return FetchedAssignment(
        moodle_course_id=7001,
        moodle_assignment_id=aid,
        course_name="資料結構",
        title=f"HW{aid}",
        due_at=due_at if due_at is not None else datetime.now(UTC) + timedelta(days=7),
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=None,
        intro_html=None,
    )


@pytest.mark.parametrize(
    "database_clock_lead",
    [timedelta(seconds=-5), timedelta(0), timedelta(seconds=5)],
    ids=["database-behind", "clocks-agree", "database-ahead"],
)
async def test_a_job_set_up_here_is_due_whichever_way_the_clocks_disagree(
    db_session, prepared_engine, test_settings, monkeypatch, database_clock_lead
):
    """Every test in this file that expects a claim relies on
    `_setup_user_job` handing it a job that is already due, and the
    executor decides that with this host's clock. So the job must not take
    its `run_after` from the database's clock: a Docker VM's clock drifts
    against its host's, and one running even a few milliseconds ahead
    leaves a job made just before the tick not yet due -- the tick claims
    nothing, and every assertion after it sees a sync that never ran.

    Pinned by running the executor on a clock that lags the database's, as
    that drift does, and on one that leads it: the job is claimed either
    way."""
    await _setup_user_job(db_session)

    real_datetime = executor_module.datetime

    class SkewedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) - database_clock_lead

    monkeypatch.setattr(executor_module, "datetime", SkewedDatetime)
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    assert await run_sync_tick(worker) == 1
    assert fetcher.calls == ["tok-1"]


async def test_stale_running_job_recovered(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(
        db_session,
        job_kwargs={
            "status": "running",
            "locked_by": "dead-worker",
            "locked_at": datetime.now(UTC) - timedelta(minutes=11),
        },
    )
    run = SyncRun(sync_job_id=job.id, user_id=user.id)
    db_session.add(run)
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    await db_session.refresh(job)
    # Recovered to pending, then immediately claimed+executed this tick.
    assert job.status == "pending"
    assert job.last_success_at is not None
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error == "stale_lock_recovered"


async def test_fresh_running_job_not_recovered_and_counts_against_cap(
    db_session, prepared_engine, test_settings
):
    await _setup_user_job(
        db_session,
        job_kwargs={
            "status": "running",
            "locked_by": "other-worker",
            "locked_at": datetime.now(UTC) - timedelta(minutes=1),
        },
    )
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []  # nothing claimable


async def test_global_concurrency_cap_limits_claims(
    db_session, prepared_engine, test_settings
):
    # 5 fresh running rows (other workers) → cap 5 reached → claim nothing.
    for i in range(5):
        await _setup_user_job(
            db_session,
            student_id=f"b1120300{i}",
            job_kwargs={
                "status": "running",
                "locked_by": "other",
                "locked_at": datetime.now(UTC),
            },
        )
    await _setup_user_job(db_session, student_id="b11203099")

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []


async def test_disabled_policy_jobs_not_claimed(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    policy = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    policy.enabled = False
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    assert fetcher.calls == []
    await db_session.refresh(job)
    assert job.status == "pending"


async def test_policy_active_window_respected(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    policy = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    policy.active_from = datetime.now(UTC) + timedelta(days=1)
    await db_session.commit()

    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "device",
    [None, {"deleted_at": datetime.now(UTC)}, {"cloud_sync_enabled": False}],
    ids=["never-signed-in", "signed-out", "sync-off"],
)
async def test_no_fetch_while_no_device_syncs_and_resume_when_one_does(
    db_session, prepared_engine, test_settings, device
):
    """tigerduck-backend#85: the job stays put, unclaimed, until a device
    signs in or turns sync on."""
    user, _, job = await _setup_user_job(db_session, syncing_device=False)
    if device is not None:
        db_session.add(
            UserDevice(
                user_id=user.id,
                client_device_id="iphone-1",
                platform="ios",
                last_seen_at=datetime.now(UTC),
                **device,
            )
        )
        await db_session.commit()
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    assert await run_sync_tick(worker) == 0
    assert fetcher.calls == []
    await db_session.refresh(job)
    assert (job.status, job.attempts) == ("pending", 0)

    if device is None:
        db_session.add(
            UserDevice(
                user_id=user.id,
                client_device_id="iphone-1",
                platform="ios",
                last_seen_at=datetime.now(UTC),
            )
        )
    else:
        await db_session.execute(
            update(UserDevice).values(deleted_at=None, cloud_sync_enabled=True)
        )
    await db_session.commit()

    assert await run_sync_tick(worker) == 1
    assert fetcher.calls == ["tok-1"]


def _phone(
    user,
    label="iphone-1",
    *,
    seen_ago=None,
    push_ago=None,
    token_kind="standard",
    status="active",
):
    """A phone of `user` with TigerSync on, last seen `seen_ago` before now
    (None for never), and a token the provider last accepted `push_ago`
    before now (None for no token)."""
    now = datetime.now(UTC)
    device = UserDevice(
        user_id=user.id,
        client_device_id=label,
        platform="ios",
        last_seen_at=None if seen_ago is None else now - seen_ago,
    )
    if push_ago is not None:
        device.push_tokens.append(
            DevicePushToken(
                provider="apns",
                token_kind=token_kind,
                token_hash=f"hash-{label}",
                token_value=f"tok-{label}",
                status=status,
                last_success_at=now - push_ago,
            )
        )
    return device


DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("phones", "claimed"),
    [
        ([{"seen_ago": timedelta(0)}], True),
        ([{"seen_ago": 89 * DAY}], True),
        ([{"seen_ago": 91 * DAY}], False),
        ([{}], False),
        ([{"seen_ago": 120 * DAY, "push_ago": 13 * DAY}], True),
        ([{"seen_ago": 120 * DAY, "push_ago": 15 * DAY}], False),
        ([{"seen_ago": 120 * DAY, "push_ago": DAY, "status": "invalidated"}], False),
        ([{"seen_ago": 120 * DAY, "push_ago": DAY, "token_kind": "push_to_start"}], False),
        ([{"seen_ago": 120 * DAY}, {"seen_ago": DAY}], True),
    ],
    ids=[
        "signed-in-just-now-no-push",
        "seen-89-days-ago",
        "seen-91-days-ago",
        "never-seen",
        "push-13-days-ago",
        "push-15-days-ago",
        "push-lately-to-a-dropped-token",
        "push-lately-to-a-live-activity-token",
        "one-fresh-one-dead",
    ],
)
async def test_a_job_runs_only_for_a_device_seen_or_reached_lately(
    db_session, prepared_engine, test_settings, phones, claimed
):
    """90 days since a device was seen (`sync_job_device_active_days`), or 14
    since a push reached it (`sync_job_device_push_days`)."""
    user, _, _ = await _setup_user_job(db_session, syncing_device=False)
    db_session.add_all(
        _phone(user, f"iphone-{index}", **phone) for index, phone in enumerate(phones)
    )
    await db_session.commit()
    fetcher = StubFetcher(results=[])

    ran = await run_sync_tick(_worker(prepared_engine, test_settings, fetcher=fetcher))

    assert (ran, fetcher.calls) == ((1, ["tok-1"]) if claimed else (0, []))


async def test_a_returning_device_gets_its_overdue_job_at_the_next_tick(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(
        db_session,
        syncing_device=False,
        job_kwargs={"run_after": datetime.now(UTC) - 3 * DAY},
    )
    phone = _phone(user, seen_ago=91 * DAY)
    db_session.add(phone)
    await db_session.commit()
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    assert await run_sync_tick(worker) == 0
    await db_session.refresh(job)
    assert (job.status, job.attempts) == ("pending", 0)

    # What a sign-in or the app's register on launch does.
    phone.last_seen_at = datetime.now(UTC)
    await db_session.commit()

    assert await run_sync_tick(worker) == 1
    assert fetcher.calls == ["tok-1"]


async def _suspend_sync(conn_or_session) -> None:
    """What the portal's Suspend sync writes."""
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await conn_or_session.execute(
        text(
            "INSERT INTO system_settings (key, value) "
            "VALUES ('moodle_suspended_until', CAST(:v AS jsonb))"
        ),
        {"v": json.dumps({"until": until})},
    )


async def test_a_suspended_sync_claims_nothing(
    db_session, prepared_engine, test_settings
):
    """tigerduck-backend#86: Suspend sync was read only once a run had
    already failed, so the worker went on calling Moodle."""
    _, _, job = await _setup_user_job(db_session)
    await _suspend_sync(db_session)
    await db_session.commit()
    fetcher = StubFetcher(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    assert await run_sync_tick(worker) == 0

    assert fetcher.calls == []
    assert (await db_session.execute(select(func.count(SyncRun.id)))).scalar_one() == 0
    await db_session.refresh(job)
    assert job.status == "pending"


async def test_a_suspend_during_a_tick_hands_back_the_jobs_not_yet_run(
    db_session, prepared_engine, test_settings
):
    await _setup_user_job(db_session, student_id="b00000001", token="tok-1")
    await _setup_user_job(db_session, student_id="b00000002", token="tok-2")

    class SuspendedWhileFetching(StubFetcher):
        async def fetch_assignments(self, *, token):
            async with prepared_engine.begin() as conn:
                await _suspend_sync(conn)
            return await super().fetch_assignments(token=token)

    fetcher = SuspendedWhileFetching(results=[])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    worker = dataclasses.replace(
        worker,
        settings=worker.settings.model_copy(update={"sync_job_min_interval_seconds": 0}),
    )

    assert await run_sync_tick(worker) == 1

    assert len(fetcher.calls) == 1
    jobs = (await db_session.execute(select(SyncJob))).scalars().all()
    assert sorted((j.status, j.attempts, j.locked_by, j.last_success_at is None) for j in jobs) == [
        ("pending", 0, None, False),
        ("pending", 0, None, True),
    ]
    runs = (await db_session.execute(select(SyncRun.status, SyncRun.error))).all()
    assert sorted(runs) == [("cancelled", "sync_suspended"), ("succeeded", None)]


async def test_successful_run_upserts_and_reschedules(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(results=[_fa(1), _fa(2)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    executed = await run_sync_tick(worker)
    assert executed == 1
    assert fetcher.calls == ["tok-1"]

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_success_at is not None
    assert job.last_error is None
    # rescheduled ~8h out (policy interval 28800s)
    assert job.run_after > datetime.now(UTC) + timedelta(hours=7)

    rows = (
        (
            await db_session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.fetched_count == 2
    assert run.changed_count == 2
    entries = (
        (
            await db_session.execute(
                select(UserChangeLog).where(UserChangeLog.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 2


async def test_a_run_links_the_assignments_to_the_class_table_by_the_enrolment(
    db_session, prepared_engine, test_settings
):
    """The assignment list names a course by its numeric id; the run reads
    the student's Moodle courses for their idnumbers, which name the term
    and the course number the class table holds."""
    user, _, _ = await _setup_user_job(db_session)
    await _publish_term(db_session)
    course = UserCourse(
        user_id=user.id,
        semester="1151",
        course_key="client:1151:CS2006301",
        course_no="CS2006301",
        course_name="資料結構",
    )
    db_session.add(course)
    await db_session.commit()
    enrolled = [
        FetchedCourse(
            moodle_course_id=course_id,
            short_name=None,
            full_name="Course",
            category_id=None,
            enrolled_user_count=None,
            idnumber=idnumber,
        )
        for course_id, idnumber in ((7001, "1151CS2006301"), (7002, "1142GE1001301"))
    ]
    fetched = [_fa(1), dataclasses.replace(_fa(2), moodle_course_id=7002)]
    worker = _worker(
        prepared_engine,
        test_settings,
        fetcher=StubFetcher(results=fetched),
        course_fetcher=StubCourseFetcher(results=enrolled),
    )

    assert await run_sync_tick(worker) == 1

    rows = await db_session.execute(
        select(
            UserAssignment.moodle_assignment_id,
            UserAssignment.user_course_id,
            UserAssignment.moodle_semester,
        ).where(UserAssignment.user_id == user.id)
    )
    assert sorted(rows.all()) == [(1, course.id, "1151"), (2, None, "1142")]


async def test_a_run_whose_enrolment_fetch_fails_stores_nothing(
    db_session, prepared_engine, test_settings
):
    """Without the courses no row can be linked, so the run fails and is
    retried like a failed list fetch."""
    user, _, job = await _setup_user_job(db_session)
    worker = _worker(
        prepared_engine,
        test_settings,
        fetcher=StubFetcher(results=[_fa(1)]),
        course_fetcher=StubCourseFetcher(errors=[MoodleUnreachable("http_503")]),
    )

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert (job.status, job.last_error) == ("pending", "sync_failed:http_503")
    stored = await db_session.scalars(
        select(UserAssignment).where(UserAssignment.user_id == user.id)
    )
    assert stored.all() == []


class _ObservingFetcher(StubFetcher):
    """Runs `observe()` from inside the Moodle call, then answers."""

    def __init__(self, observe, results):
        super().__init__(results=results)
        self.observe = observe

    async def fetch_assignments(self, *, token):
        await self.observe()
        return await super().fetch_assignments(token=token)


async def test_the_list_fetch_waits_on_moodle_without_the_job_lock(
    db_session, prepared_engine, test_settings
):
    """Moodle can take its full fetch timeout; the job's row lock and a
    pooled connection must not be held while it does."""
    _, _, job = await _setup_user_job(db_session)
    seen: dict = {}

    async def observe():
        async with prepared_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM sync_jobs WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": job.id},
                )
            ).first()
            seen["lock"] = "acquired" if row is not None else "no row"
            await conn.rollback()
            seen["idle_in_transaction"] = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND state LIKE 'idle in transaction%' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
            await conn.rollback()

    fetcher = _ObservingFetcher(observe, results=[_fa(1)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)

    assert seen == {"lock": "acquired", "idle_in_transaction": 0}
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_success_at is not None


async def test_a_job_taken_back_while_the_list_fetch_ran_stores_nothing(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(db_session)

    async def recover_the_job():
        async with prepared_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE sync_jobs SET status = 'pending', locked_by = NULL, "
                    "locked_at = NULL, last_error = 'sync_failed:stale_lock' "
                    "WHERE id = :id"
                ),
                {"id": job.id},
            )

    fetcher = _ObservingFetcher(recover_the_job, results=[_fa(1)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.last_error == "sync_failed:stale_lock"
    assert job.last_success_at is None
    stored = await db_session.scalar(
        select(func.count()).select_from(UserAssignment).where(
            UserAssignment.user_id == user.id
        )
    )
    assert stored == 0


async def test_a_failure_after_the_job_was_reclaimed_leaves_the_new_owner_alone(
    db_session, prepared_engine, test_settings
):
    """A fetch that outlasts the stale-lock window can see its job recovered
    and claimed by another worker; its failure is then not this run's to
    record."""
    _, _, job = await _setup_user_job(db_session)

    class ReclaimedThenUnreachable(StubFetcher):
        async def fetch_assignments(self, *, token):
            async with prepared_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE sync_jobs SET locked_by = 'worker-b', "
                        "locked_at = now() WHERE id = :id"
                    ),
                    {"id": job.id},
                )
            raise MoodleUnreachable("timeout")

    worker = _worker(prepared_engine, test_settings, fetcher=ReclaimedThenUnreachable())

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "running"
    assert job.locked_by == "worker-b"
    assert job.attempts == 0
    assert job.last_error is None


async def test_expired_token_disables_job_and_queues_push(
    db_session, prepared_engine, test_settings
):
    """Token-only: an expired Moodle token disables the sync job and
    queues a push notification. No password-based refresh — the user must
    open the app to send a fresh token via PATCH /auth/credentials."""
    user, account, job = await _setup_user_job(db_session)
    # The push is queued only when the account has an iPhone or iPad with
    # course sync on (spec §4.5) — the reauth prompt asks the user to reopen
    # the app, and nothing else can act on it. Added here rather than in
    # `_setup_user_job` because this is the only executor test that reaches
    # the notification path. The gate itself is covered by
    # `test_reauth_push.py`.
    db_session.add(
        UserDevice(
            user_id=user.id,
            client_device_id="iphone-1",
            platform="ios",
            cloud_sync_enabled=True,
        )
    )
    await db_session.commit()

    fetcher = StubFetcher(errors=[MoodleTokenInvalid("dead")])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "disabled"
    assert job.last_error == "credential_invalid"
    await db_session.refresh(account)
    assert account.credential_status == "invalid"
    push = (
        await db_session.execute(
            select(PushJob).where(PushJob.user_id == user.id)
        )
    ).scalar_one()
    assert push.scenario == "reauth_required"


async def test_a_token_stored_while_moodle_rejected_the_old_one_stays_valid(
    db_session, prepared_engine, test_settings
):
    """The fetch runs with no transaction open, so the app can store a fresh
    token through PATCH /v3/auth/credentials meanwhile. Moodle rejecting the
    old token must not invalidate the fresh one or disable the jobs."""
    user, account, job = await _setup_user_job(db_session)

    class FreshTokenStoredThenRejected(StubFetcher):
        async def fetch_assignments(self, *, token):
            fresh = _cipher().encrypt(
                {"token_cache": {"moodle_token": "tok-2"}},
                build_credential_aad(account.id, "ntust_sso"),
            )
            async with prepared_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE external_account_credentials SET "
                        "ciphertext = :c, nonce = :n WHERE external_account_id = :a"
                    ),
                    {"c": fresh.ciphertext, "n": fresh.nonce, "a": account.id},
                )
            raise MoodleTokenInvalid("dead")

    worker = _worker(
        prepared_engine, test_settings, fetcher=FreshTokenStoredThenRejected()
    )
    await run_sync_tick(worker)

    await db_session.refresh(account)
    assert account.credential_status == "active"
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.locked_by is None
    pushes = (
        await db_session.execute(select(PushJob).where(PushJob.user_id == user.id))
    ).scalars().all()
    assert pushes == []


async def test_a_token_stored_right_after_the_run_read_its_own_stays_valid(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The nonce a rejection compares must come from the row the token was
    decrypted from. A fresh token stored just after that read must not be
    taken for the one Moodle rejected, as a second read would."""
    user, account, job = await _setup_user_job(db_session)
    real_load = executor_module.load_credential

    async def load_then_fresh_token_stored(session, cipher, *, external_account_id):
        loaded = await real_load(
            session, cipher, external_account_id=external_account_id
        )
        fresh = _cipher().encrypt(
            {"token_cache": {"moodle_token": "tok-2"}},
            build_credential_aad(account.id, "ntust_sso"),
        )
        async with prepared_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE external_account_credentials SET "
                    "ciphertext = :c, nonce = :n WHERE external_account_id = :a"
                ),
                {"c": fresh.ciphertext, "n": fresh.nonce, "a": account.id},
            )
        return loaded

    monkeypatch.setattr(
        executor_module, "load_credential", load_then_fresh_token_stored
    )
    fetcher = StubFetcher(errors=[MoodleTokenInvalid("dead")])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)

    assert fetcher.calls == ["tok-1"]
    await db_session.refresh(account)
    assert account.credential_status == "active"
    await db_session.refresh(job)
    assert job.status == "pending"


async def _run_when_due(db_session, worker, job) -> None:
    job.run_after = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await run_sync_tick(worker)
    await db_session.refresh(job)


@pytest.mark.parametrize(
    ("outage", "last_error"),
    [
        (MoodleUnreachable("http_503"), "sync_failed:http_503"),
        (SsoUnavailable("http_502"), "sync_failed:http_502"),
        (MoodleRateLimited("http_429"), "school_rate_limited"),
    ],
    ids=["moodle-unreachable", "sso-unavailable", "rate-limited"],
)
async def test_an_outage_longer_than_the_retries_never_fails_the_job_for_good(
    db_session, prepared_engine, test_settings, outage, last_error
):
    """An outage that outlasts the retries, about 15 minutes with the
    default backoff, left the job failed, and a failed job waits for an
    operator: no released app calls run-now, and re-login revives only a
    disabled job. Past its attempts the job runs again at its policy
    interval, with its attempts reset, so the next error gets every retry
    again, and at its policy's priority rather than the 1 a run-now gave
    it."""
    _, _, job = await _setup_user_job(db_session, job_kwargs={"priority": 1})
    fetcher = StubFetcher(errors=[outage] * (job.max_attempts + 1))
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    interval, priority = (
        await db_session.execute(
            select(SyncPolicy.default_interval_seconds, SyncPolicy.priority).where(
                SyncPolicy.job_type == job.job_type
            )
        )
    ).one()
    assert priority != 1

    for _ in range(job.max_attempts):
        await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts, job.priority) == ("pending", 0, priority)
    assert job.last_error == last_error
    assert job.run_after - job.last_failure_at == timedelta(seconds=interval)
    assert await run_sync_tick(worker) == 0

    # Still down when the interval is up: back on the backoff.
    await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts) == ("pending", 1)
    assert job.run_after - job.last_failure_at == timedelta(
        seconds=worker.settings.sync_job_backoff_base_seconds
    )

    await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts, job.last_error) == ("pending", 0, None)
    assert job.last_success_at is not None
    assert fetcher.errors == []


def _a_crash(monkeypatch, attempts):
    errors = [RuntimeError("unhandled answer") for _ in range(attempts)]
    return StubFetcher(errors=errors), "sync_failed:RuntimeError"


def _a_failed_submission_write(monkeypatch, attempts):
    async def poisoning_select(session, *, user_id, now, window_hours):
        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(
        "server.syncjobs.executor.select_assignment_ids", poisoning_select
    )
    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    return fetcher, "sync_failed:submission_status"


@pytest.mark.parametrize(
    "arrange",
    [_a_crash, _a_failed_submission_write],
    ids=["crash", "submission-status-db-error"],
)
async def test_an_error_retrying_cannot_fix_still_fails_the_job(
    db_session, prepared_engine, test_settings, monkeypatch, arrange
):
    """A crash in our code, or a database error writing what the
    submission probe found, comes back on every run. Past its attempts the
    job is failed, for an operator to see and retry once it is fixed."""
    _, _, job = await _setup_user_job(db_session)
    fetcher, last_error = arrange(monkeypatch, job.max_attempts)
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts) == ("pending", 1)
    for _ in range(job.max_attempts - 1):
        await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts) == ("failed", job.max_attempts)
    assert job.last_error == last_error


async def test_an_answer_that_lost_every_course_keeps_the_assignments(
    db_session, prepared_engine, test_settings
):
    """Moodle answers no course and a course warning when it refuses every
    course (an unaccepted site policy, for one). Applied as an empty fetch,
    that answer soft-deleted all of the user's assignments."""
    user, _, job = await _setup_user_job(db_session)
    db_session.add(
        UserAssignment(
            user_id=user.id, moodle_course_id=7001, moodle_assignment_id=1, title="HW1"
        )
    )
    await db_session.commit()
    body = {
        "courses": [],
        "warnings": [
            {
                "item": "course",
                "itemid": 7001,
                "warningcode": "1",
                "message": "No access rights in course context",
            }
        ],
    }
    fetcher = HttpAssignmentFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "sync_failed:no_course_access"
    row = (
        await db_session.execute(
            select(UserAssignment)
            .where(UserAssignment.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.deleted_at is None


async def test_an_answer_that_lost_every_course_withdraws_a_course_it_left_out(
    db_session, prepared_engine, test_settings
):
    """Moodle leaves out a course the user is a TA in without a warning. An
    answer that refused every other course kept that course's rows, fetched
    before the capability filter, so their reminders went on. The refused
    course and the placeholder, which names no course, keep theirs."""
    user, _, job = await _setup_user_job(db_session, student_id="b00000001")
    device = UserDevice(
        user_id=user.id,
        client_device_id="iphone-1",
        platform="ios",
        app_version="2.1.0",
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-iphone-1",
            token_value="tok-iphone-1",
        )
    )
    due_at = datetime.now(UTC) + timedelta(hours=30)
    await _publish_term(db_session)
    rows = {
        "refused": UserAssignment(
            user_id=user.id,
            moodle_course_id=7001,
            moodle_assignment_id=1,
            moodle_semester="1151",
            title="HW1",
            due_at=due_at,
        ),
        "ta_course": UserAssignment(
            user_id=user.id,
            moodle_course_id=7003,
            moodle_assignment_id=2,
            moodle_semester="1151",
            title="HW2",
            due_at=due_at,
        ),
        "placeholder": UserAssignment(
            user_id=user.id,
            moodle_course_id=0,
            moodle_assignment_id=3,
            course_no="CS0000001",
            title="HW3",
            due_at=due_at,
        ),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    session_factory = build_session_factory(prepared_engine)
    assert await scan_assignment_reminders(session_factory, test_settings) == 6
    body = {
        "courses": [],
        "warnings": [{"item": "course", "itemid": 7001, "warningcode": "1"}],
    }
    fetcher = HttpAssignmentFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )

    await run_sync_tick(_worker(prepared_engine, test_settings, fetcher=fetcher))
    await scan_assignment_reminders(session_factory, test_settings)

    await db_session.refresh(job)
    assert (job.status, job.attempts, job.last_error) == (
        "pending",
        1,
        "sync_failed:no_course_access",
    )
    for row in rows.values():
        await db_session.refresh(row)
    assert [name for name, row in rows.items() if row.deleted_at] == ["ta_course"]
    # The run history, which the portal and Grafana read, counts the delete.
    run = (
        await db_session.execute(select(SyncRun).where(SyncRun.sync_job_id == job.id))
    ).scalar_one()
    assert (run.status, run.error, run.fetched_count, run.changed_count) == (
        "failed",
        "sync_failed:no_course_access",
        0,
        1,
    )
    reminders = (
        (
            await db_session.execute(
                select(PushJob)
                .where(PushJob.user_id == user.id, PushJob.channel == REMINDER_CHANNEL)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    statuses: dict[int, set[str]] = {}
    for reminder in reminders:
        statuses.setdefault(reminder.payload["moodle_assignment_id"], set()).add(
            reminder.status
        )
    assert statuses == {1: {"pending"}, 2: {"cancelled"}, 3: {"pending"}}
    # The user's devices are told to sync, and the change log has the delete.
    entries = (
        await db_session.execute(
            select(UserChangeLog.operation, UserChangeLog.entity_id).where(
                UserChangeLog.user_id == user.id
            )
        )
    ).all()
    assert entries == [("delete", str(rows["ta_course"].id))]
    triggers = (
        await db_session.execute(
            select(func.count())
            .select_from(PushJob)
            .where(PushJob.user_id == user.id, PushJob.scenario == "sync_trigger")
        )
    ).scalar_one()
    assert triggers == 1
    # A failed run otherwise changes nothing, so the user's activity log
    # says what this one deleted.
    details = (
        await db_session.execute(
            select(SyncLogEntry.detail).where(
                SyncLogEntry.user_id == user.id,
                SyncLogEntry.message.startswith("Moodle refused"),
            )
        )
    ).scalars().all()
    assert details == [{"refused_course_ids": [7001], "deleted": 1}]


async def test_a_database_error_while_dropping_the_courses_left_out_drops_nothing(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The deletes, their change log entries, the sync trigger and the run's
    counts commit together or not at all. The run then fails as our own
    error, so a lasting one ends the job failed for an operator rather than
    hiding behind the refusal."""
    user, _, job = await _setup_user_job(db_session, student_id="b00000001")
    ta_row = UserAssignment(
        user_id=user.id, moodle_course_id=7003, moodle_assignment_id=2, title="HW2"
    )
    db_session.add(ta_row)
    await db_session.commit()

    async def failing_trigger(session, user_id, *, source=None):
        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(executor_module, "enqueue_sync_trigger", failing_trigger)
    body = {
        "courses": [],
        "warnings": [{"item": "course", "itemid": 7001, "warningcode": "1"}],
    }
    fetcher = HttpAssignmentFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )

    await run_sync_tick(_worker(prepared_engine, test_settings, fetcher=fetcher))

    await db_session.refresh(ta_row)
    assert ta_row.deleted_at is None
    changes = (
        await db_session.execute(
            select(func.count())
            .select_from(UserChangeLog)
            .where(UserChangeLog.user_id == user.id)
        )
    ).scalar_one()
    assert changes == 0
    await db_session.refresh(job)
    assert (job.status, job.attempts, job.last_error) == (
        "pending",
        1,
        "sync_failed:DBAPIError",
    )
    run = (
        await db_session.execute(select(SyncRun).where(SyncRun.sync_job_id == job.id))
    ).scalar_one()
    assert (run.status, run.fetched_count, run.changed_count) == ("failed", None, None)


@pytest.mark.parametrize(
    "body, code",
    [
        (
            {
                "courses": [],
                "warnings": [{"item": "course", "itemid": 7001, "warningcode": "1"}],
            },
            "no_course_access",
        ),
        ({"warnings": []}, "unexpected_shape"),
    ],
    ids=["no-course-access", "unexpected-shape"],
)
async def test_an_answer_without_the_courses_never_fails_the_job_for_good(
    db_session, prepared_engine, test_settings, body, code
):
    """Its cause, such as a site policy the user has not accepted, can last
    until the user acts on the Moodle site. A failed job waits for an
    operator: no released app calls run-now, and re-login revives only a
    disabled job. Past its attempts the job runs at its policy interval,
    with its attempts reset for the errors that may come next, and at its
    policy's priority rather than the 1 a run-now gave it."""
    user, _, job = await _setup_user_job(db_session, job_kwargs={"priority": 1})
    answers = [body] * job.max_attempts + [503, {"courses": [], "warnings": []}]

    def answer(request: httpx.Request) -> httpx.Response:
        next_answer = answers.pop(0)
        if isinstance(next_answer, int):
            return httpx.Response(next_answer)
        return httpx.Response(200, json=next_answer)

    fetcher = HttpAssignmentFetcher(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(answer),
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    interval, priority = (
        await db_session.execute(
            select(SyncPolicy.default_interval_seconds, SyncPolicy.priority).where(
                SyncPolicy.job_type == job.job_type
            )
        )
    ).one()
    assert priority != 1

    for _ in range(job.max_attempts):
        await _run_when_due(db_session, worker, job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.priority == priority
    assert job.last_error == f"sync_failed:{code}"
    assert job.run_after > datetime.now(UTC) + timedelta(seconds=interval - 60)

    await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts) == ("pending", 1)
    await _run_when_due(db_session, worker, job)
    assert (job.status, job.attempts, job.last_error) == ("pending", 0, None)
    assert answers == []


async def test_rate_limited_records_school_rate_limited(
    db_session, prepared_engine, test_settings
):
    _, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(errors=[MoodleRateLimited("429")])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    await run_sync_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "school_rate_limited"


async def test_assignment_sync_probes_submission_status(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """After the assignment list lands, the narrow window gets probed."""
    await _setup_user_job(db_session)
    probed: list[list[int]] = []

    async def fake_probe(*, token, assignment_ids, max_concurrency, **_):
        probed.append(list(assignment_ids))
        return {}

    # Due inside the default 48h probe window, unsubmitted — in scope for
    # select_assignment_ids so the probe actually has something to fetch.
    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", fake_probe, raising=False
    )

    await run_sync_tick(worker)

    assert probed, "submission status was never probed"
    assert probed == [[1]]


async def test_probe_failure_does_not_fail_the_run(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A dead probe must not take the assignment sync down with it."""
    _, _, job = await _setup_user_job(db_session)

    async def exploding_probe(*, token, assignment_ids, max_concurrency, **_):
        raise RuntimeError("moodle is having a day")

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", exploding_probe, raising=False
    )

    await run_sync_tick(worker)

    # The run still succeeds — assert against the SyncRun row the same way
    # the existing success-path tests in this file do.
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_success_at is not None
    assert job.last_error is None
    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "succeeded"


async def test_submission_probe_token_invalid_disables_job(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """An invalid token discovered while probing submissions must still
    disable the job, the same outcome as an invalid token surfacing from
    the assignment fetch itself — every subsequent Moodle call would fail
    identically either way."""
    _, account, job = await _setup_user_job(db_session)

    async def dead_token_probe(*, token, assignment_ids, max_concurrency, **_):
        raise MoodleTokenInvalid("invalidtoken")

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", dead_token_probe, raising=False
    )

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "disabled"
    assert job.last_error == "credential_invalid"
    await db_session.refresh(account)
    assert account.credential_status == "invalid"


async def test_submission_probe_rate_limit_reaches_backoff(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A rate limit on the whole probe is not a single assignment's
    failure -- every remaining probe would fail identically, so it must
    reach the same backoff `fetch_assignments` already gets, not be
    swallowed as routine probe noise."""
    _, _, job = await _setup_user_job(db_session)

    async def rate_limited_probe(*, token, assignment_ids, max_concurrency, **_):
        raise MoodleRateLimited("http_429")

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", rate_limited_probe, raising=False
    )

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "school_rate_limited"
    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "failed"


async def test_submission_probe_db_error_keeps_assignment_sync_and_backs_off(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A genuine database error while refreshing submission status (not a
    Moodle call) must not undo the assignment list sync, which has already
    committed by then -- but it must fail the run and back the job off. A
    run recorded as a success would put the next probe a whole policy
    interval away, and until then an assignment already submitted on
    Moodle keeps its reminder queued and its Live Activity counting down."""
    user, _, job = await _setup_user_job(db_session)

    async def poisoning_select(session, *, user_id, now, window_hours):
        # A real database-level error, not a mocked Python exception -- so
        # this proves the probe's failed transaction is contained, rather
        # than merely that some exception gets caught somewhere.
        await session.execute(text("SELECT 1/0"))
        return []  # pragma: no cover - unreachable, the line above raises

    monkeypatch.setattr(
        "server.syncjobs.executor.select_assignment_ids", poisoning_select
    )

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    before = datetime.now(UTC)
    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "sync_failed:submission_status"
    # Retried on the failure backoff, not at the policy's next interval.
    assert job.run_after <= before + timedelta(
        seconds=worker.settings.sync_job_backoff_cap_seconds + 60
    )

    rows = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == 1  # apply_fetched_assignments's write survived

    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "failed"


async def test_submission_status_settings_are_threaded_through(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A non-default window/concurrency setting must actually reach
    select_assignment_ids and the fetcher, not just exist in config."""
    await _setup_user_job(db_session)
    seen: dict = {}

    async def recording_probe(*, token, assignment_ids, max_concurrency, **_):
        seen["assignment_ids"] = list(assignment_ids)
        seen["max_concurrency"] = max_concurrency
        return {}

    # Due in 84h: inside a widened 96h window, outside the *default* 72h
    # window -- proves window_hours actually comes from settings rather
    # than being hardcoded to the default.
    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=84))]
    )
    settings = test_settings.model_copy(
        update={
            "submission_status_window_hours": 96,
            "submission_status_max_concurrency": 9,
        }
    )
    worker = _worker(prepared_engine, settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", recording_probe, raising=False
    )

    await run_sync_tick(worker)

    assert seen["assignment_ids"] == [1]
    assert seen["max_concurrency"] == 9


async def test_the_probe_reaches_the_largest_reminder_a_sync_ahead():
    """The 48 h reminder, the largest the apps offer, fires 48 h before the
    due time. The probe has to reach the assignment at least one assignment
    sync before then, or a submission made earlier is never seen and the
    reminder goes out anyway."""
    largest_reminder_hours = 48
    assignments = next(
        policy
        for policy in DEFAULT_POLICIES
        if policy["job_type"] == SyncJobType.moodle_assignments.value
    )
    window = Settings.model_fields["submission_status_window_hours"].default
    assert window >= largest_reminder_hours + assignments["default_interval_seconds"] / 3600




@pytest.mark.parametrize(
    ("escalation", "last_error"),
    [
        (MoodleRateLimited("http_429"), "school_rate_limited"),
        (
            MoodleUnreachable("submission_probe_unreachable"),
            "sync_failed:submission_probe_unreachable",
        ),
    ],
    ids=["rate-limited", "unreachable"],
)
async def test_a_probe_escalation_keeps_the_committed_list_sync(
    db_session, prepared_engine, test_settings, monkeypatch, escalation, last_error
):
    """An escalation from the submission probe backs the job off exactly as
    before -- the run fails, the job waits with the error -- but the
    assignment list sync already committed, so its rows and changelog
    entries stay. Probing inside the list sync's transaction let one 429
    on the probe roll back a list sync that had succeeded."""
    user, _, job = await _setup_user_job(db_session)

    async def escalating_probe(*, token, assignment_ids, max_concurrency, **_):
        raise escalation

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", escalating_probe, raising=False
    )

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == last_error
    run = (
        await db_session.execute(
            select(SyncRun).where(SyncRun.sync_job_id == job.id)
        )
    ).scalar_one()
    assert run.status == "failed"

    rows = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalars().all()
    assert [r.moodle_assignment_id for r in rows] == [1]
    changes = (
        await db_session.execute(
            select(UserChangeLog).where(UserChangeLog.user_id == user.id)
        )
    ).scalars().all()
    assert [(c.entity_type, c.entity_id, c.operation) for c in changes] == [
        ("assignment", str(rows[0].id), "upsert")
    ]


async def test_the_probe_waits_on_moodle_without_the_sync_state_lock(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """Every client write for a user queues behind that user's
    `user_sync_state` row lock (`sync.changelog.lock_sync_state`), and the
    assignment list sync takes it. By the time the probe waits on Moodle --
    a site-info call plus a request per assignment, each allowed the full
    fetch timeout -- that lock must be released and the list sync
    committed.

    Checked from a second connection inside the stubbed Moodle call: it
    takes the lock with NOWAIT, sees the synced row, and finds no other
    connection idle inside a transaction. The sync-state row exists before
    the run, as it does for anyone who has synced before, so a lock still
    held would show up as a conflict rather than as a missing row."""
    user, _, _ = await _setup_user_job(db_session)
    db_session.add(UserSyncState(user_id=user.id))
    await db_session.commit()
    seen: dict = {}

    async def observing_probe(*, token, assignment_ids, max_concurrency, **_):
        async with prepared_engine.connect() as conn:
            try:
                row = (
                    await conn.execute(
                        text(
                            "SELECT user_id FROM user_sync_state "
                            "WHERE user_id = :u FOR UPDATE NOWAIT"
                        ),
                        {"u": user.id},
                    )
                ).first()
                seen["lock"] = "acquired" if row is not None else "no row"
            except Exception as exc:  # lock_not_available while it is held
                seen["lock"] = f"blocked: {type(exc).__name__}"
            await conn.rollback()
            seen["rows"] = (
                await conn.execute(
                    text("SELECT count(*) FROM user_assignments WHERE user_id = :u"),
                    {"u": user.id},
                )
            ).scalar_one()
            seen["idle_in_transaction"] = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND state LIKE 'idle in transaction%' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
            await conn.rollback()
        return {}

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", observing_probe, raising=False
    )

    await run_sync_tick(worker)

    assert seen == {"lock": "acquired", "rows": 1, "idle_in_transaction": 0}


async def test_a_job_taken_back_while_the_probe_ran_is_left_to_its_new_owner(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The run's success is recorded in a transaction of its own after the
    probe, so it re-checks that the job is still this worker's: stale-lock
    recovery may have taken it back while the probe waited on Moodle, and
    then the recovery's bookkeeping must stand."""
    _, _, job = await _setup_user_job(db_session)

    async def probe_while_the_job_is_recovered(
        *, token, assignment_ids, max_concurrency, **_
    ):
        async with prepared_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE sync_jobs SET status = 'pending', locked_by = NULL, "
                    "locked_at = NULL, last_error = 'sync_failed:stale_lock' "
                    "WHERE id = :id"
                ),
                {"id": job.id},
            )
        return {}

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher,
        "fetch_submission_status",
        probe_while_the_job_is_recovered,
        raising=False,
    )

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_error == "sync_failed:stale_lock"
    assert job.last_success_at is None


@pytest.fixture
def executor_logs_through_the_production_chain(monkeypatch):
    """Log through the chain `logging_setup.configure` builds in production.

    The same approach as `production_log_chain` in
    test_moodle_submission_status.py: call the real `configure`, save and
    restore the process-global logger state it changes, and replace the
    module's logger proxy -- which froze its bound logger on first use,
    under whatever an earlier test configured -- with one created after
    `configure`.
    """
    names = ("httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    root = logging.getLogger().level
    try:
        # Production settings refuse to load without a shared secret. Name
        # one here rather than leave it to a local `.env`, which neither a
        # clean checkout nor CI has.
        configure(
            Settings(
                env="production",
                log_level="INFO",
                api_shared_secret="test-shared-secret",
            )
        )
        monkeypatch.setattr(
            executor_module,
            "logger",
            structlog.get_logger("server.syncjobs.executor"),
        )
        yield
    finally:
        structlog.reset_defaults()
        for n, level in saved.items():
            logging.getLogger(n).setLevel(level)
        logging.getLogger().setLevel(root)


async def test_an_unexpected_probe_failure_is_logged_without_the_token(
    db_session,
    prepared_engine,
    test_settings,
    monkeypatch,
    capsys,
    executor_logs_through_the_production_chain,
):
    """The executor's handler around the whole submission fetch logs an
    unanticipated exception with its traceback, from a frame that holds the
    Moodle token, and `exc_info` renders the message of every exception in
    the `__cause__`/`__context__` chain. So an exception whose message
    carries the token -- inside a full webservice URL, chained under another
    that carries it too -- must come out scrubbed.

    The messages are built outside the raising function: a traceback also
    prints each frame's source line, and an f-string at the `raise` would
    put `wstoken=` in the output from this test's own source.
    """
    token = "tok_executor_leak_canary"
    await _setup_user_job(db_session, token=token)
    leaky_url = (
        "https://moodle.example.test/webservice/rest/server.php"
        f"?wstoken={token}&wsfunction=x"
    )
    inner_message = f"upstream refused {leaky_url}"
    outer_message = f"probe blew up handling {leaky_url}"

    async def leaky_probe(*, token, assignment_ids, max_concurrency, **_):
        try:
            raise ValueError(inner_message)
        except ValueError:
            raise RuntimeError(outer_message)

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", leaky_probe, raising=False
    )

    await run_sync_tick(worker)

    stdout = capsys.readouterr().out
    assert token not in stdout, "the Moodle token reached the log output"
    lines = [
        line for line in stdout.splitlines() if "submissions.refresh_failed" in line
    ]
    assert len(lines) == 1, lines
    payload = json.loads(lines[0])
    assert payload["error"] == "RuntimeError"
    traceback_text = payload["exception"]
    # Scrubbed, not dropped: both messages are still rendered.
    assert "probe blew up handling" in traceback_text
    assert "upstream refused" in traceback_text
    assert traceback_text.count("wstoken=<token-redacted>") == 2


async def test_a_submission_the_probe_finds_cancels_the_pending_reminder(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """Through the whole executor: the probe reports an assignment
    submitted, the row is marked submitted, and the reminder still waiting
    for it is cancelled. Every other probe test here returns `{}` or
    raises, so without this nothing notices if the executor stops handing
    the probe's result to `apply_submission_status`, or that answer on to
    `cancel_for_submitted`."""
    user, _, _ = await _setup_user_job(db_session)
    due_at = datetime.now(UTC) + timedelta(hours=10)
    reminder = PushJob(
        user_id=user.id,
        dedupe_key=_dedupe_key(1, 2.0, int(due_at.timestamp())),
        channel=REMINDER_CHANNEL,
        scenario="reminder_2h",
        fire_at=due_at - timedelta(hours=2),
        payload={"kind": "assignment_reminder", "moodle_assignment_id": 1},
    )
    db_session.add(reminder)
    await db_session.commit()
    submitted_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

    async def submitted_probe(*, token, assignment_ids, max_concurrency, **_):
        return {
            aid: FetchedSubmission(
                moodle_assignment_id=aid, is_submitted=True, submitted_at=submitted_at
            )
            for aid in assignment_ids
        }

    fetcher = StubFetcher(results=[_fa(1, due_at=due_at)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", submitted_probe, raising=False
    )

    await run_sync_tick(worker)

    row = (
        await db_session.execute(
            select(UserAssignment)
            .where(UserAssignment.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.provider_is_submitted is True
    assert row.provider_submitted_at == submitted_at
    await db_session.refresh(reminder)
    assert reminder.status == "cancelled"


async def test_a_submission_the_probe_finds_triggers_a_sync_of_its_own(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The list sync's trigger can go out before the probe commits, so the
    probe's flip gets a trigger of its own rather than sharing that one's
    dedupe key and being dropped as a duplicate of it."""
    user, _, _ = await _setup_user_job(db_session)
    due_at = datetime.now(UTC) + timedelta(hours=10)

    async def submitted_probe(*, token, assignment_ids, max_concurrency, **_):
        return {
            aid: FetchedSubmission(
                moodle_assignment_id=aid, is_submitted=True, submitted_at=None
            )
            for aid in assignment_ids
        }

    fetcher = StubFetcher(results=[_fa(1, due_at=due_at)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(
        worker.fetcher, "fetch_submission_status", submitted_probe, raising=False
    )

    await run_sync_tick(worker)

    keys = (
        await db_session.execute(
            select(PushJob.dedupe_key).where(
                PushJob.user_id == user.id, PushJob.scenario == "sync_trigger"
            )
        )
    ).scalars().all()
    # One from the list sync (it inserted the assignment), one from the probe.
    assert len(keys) == 2
    assert [k for k in keys if k.endswith(":submissions")] != []


async def test_a_sync_reminds_from_the_students_extension_and_keeps_it_when_the_probe_fails(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The probe's answer moves the due date to the student's extension in
    the same run as the list sync, and a later run whose probe fails keeps
    it rather than going back to Moodle's date."""
    user, _, job = await _setup_user_job(db_session)
    due_at = (datetime.now(UTC) + timedelta(hours=10)).replace(microsecond=0)
    extension = due_at + timedelta(days=2)
    answers = [
        {
            1: FetchedSubmission(
                moodle_assignment_id=1,
                is_submitted=False,
                submitted_at=None,
                extension_due_at=extension,
            )
        },
        MoodleUnreachable("submission_probe_unreachable"),
    ]

    async def probe(*, token, assignment_ids, max_concurrency, **_):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    fetcher = StubFetcher(results=[_fa(1, due_at=due_at)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(worker.fetcher, "fetch_submission_status", probe, raising=False)

    async def stored():
        return (
            await db_session.execute(
                select(UserAssignment.due_at, UserAssignment.extension_due_at)
                .where(UserAssignment.user_id == user.id)
                .execution_options(populate_existing=True)
            )
        ).one()

    await run_sync_tick(worker)
    assert tuple(await stored()) == (extension, extension)
    keys = (
        await db_session.execute(
            select(PushJob.dedupe_key).where(
                PushJob.user_id == user.id, PushJob.scenario == "sync_trigger"
            )
        )
    ).scalars().all()
    assert [k for k in keys if k.endswith(":submissions")] != []

    await _run_when_due(db_session, worker, job)
    assert answers == []
    assert tuple(await stored()) == (extension, extension)


@pytest.mark.parametrize(
    "answer,kept",
    [("extension", True), ("no-extension", False), ("no-answer", False)],
)
async def test_a_date_the_list_moves_back_is_probed_and_the_answer_decides(
    db_session, prepared_engine, test_settings, monkeypatch, answer, kept
):
    """An app's initial upload stored the student's later date on a row with
    a real course id, or a teacher moved the due date earlier. Both are past
    the probe window, and the probe is asked anyway: an extension it gives
    keeps the later date, and without one Moodle's date stands."""
    user, _, _ = await _setup_user_job(db_session)
    moodle_due = (datetime.now(UTC) + timedelta(days=5)).replace(microsecond=0)
    later = moodle_due + timedelta(days=2)
    db_session.add(
        UserAssignment(
            user_id=user.id,
            moodle_course_id=7001,
            moodle_assignment_id=1,
            title="HW1",
            due_at=later,
        )
    )
    await db_session.commit()
    probed: list[list[int]] = []

    async def probe(*, token, assignment_ids, max_concurrency, **_):
        probed.append(list(assignment_ids))
        if answer == "no-answer":
            return {}
        return {
            1: FetchedSubmission(
                moodle_assignment_id=1,
                is_submitted=False,
                submitted_at=None,
                extension_due_at=later if answer == "extension" else None,
            )
        }

    fetcher = StubFetcher(results=[_fa(1, due_at=moodle_due)])
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(worker.fetcher, "fetch_submission_status", probe, raising=False)

    await run_sync_tick(worker)

    assert probed == [[1]]
    due_at = (
        await db_session.execute(
            select(UserAssignment.due_at)
            .where(UserAssignment.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert due_at == (later if kept else moodle_due)


async def _display_name(db_session, user) -> str | None:
    user_id = user.id
    db_session.expire_all()
    return await db_session.scalar(
        select(User.display_name).where(User.id == user_id)
    )


async def _setup_course_job(db_session, *, display_name=None):
    user, _, job = await _setup_user_job(db_session)
    job.job_type = SyncJobType.ntust_courses.value
    user.display_name = display_name
    await db_session.execute(
        update(SyncPolicy)
        .where(SyncPolicy.job_type == SyncJobType.ntust_courses.value)
        .values(enabled=True)
    )
    await db_session.commit()
    return user


async def test_a_course_sync_fills_in_a_missing_name(
    db_session, prepared_engine, test_settings
):
    user = await _setup_course_job(db_session)
    worker = _worker(
        prepared_engine,
        test_settings,
        course_fetcher=StubCourseFetcher(fullname="  Test Student  "),
    )

    assert await run_sync_tick(worker) == 1

    assert await _display_name(db_session, user) == "Test Student"


async def test_a_course_sync_without_a_name_keeps_the_stored_one(
    db_session, prepared_engine, test_settings
):
    user = await _setup_course_job(db_session, display_name="Test Student")
    worker = _worker(
        prepared_engine,
        test_settings,
        course_fetcher=StubCourseFetcher(fullname="   "),
    )

    assert await run_sync_tick(worker) == 1

    assert await _display_name(db_session, user) == "Test Student"


async def test_the_submission_probe_fills_in_a_missing_name(
    db_session, prepared_engine, test_settings, monkeypatch
):
    user, _, _ = await _setup_user_job(db_session)

    async def probe(*, token, assignment_ids, max_concurrency, on_fullname):
        on_fullname("Test Student")
        return {}

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(worker.fetcher, "fetch_submission_status", probe, raising=False)

    await run_sync_tick(worker)

    assert await _display_name(db_session, user) == "Test Student"


# Postgres refuses a NUL in text, so the name write fails for real.
_REFUSED_NAME = "Test\x00Student"


async def test_a_name_the_database_refuses_keeps_the_course_sync(
    db_session, prepared_engine, test_settings, monkeypatch
):
    user = await _setup_course_job(db_session)
    user_id = user.id
    course = FetchedCourse(
        moodle_course_id=7001,
        short_name="DS",
        full_name="Data Structures",
        category_id=None,
        enrolled_user_count=None,
    )
    worker = _worker(
        prepared_engine,
        test_settings,
        course_fetcher=StubCourseFetcher(results=[course], fullname=_REFUSED_NAME),
    )
    monkeypatch.setattr(
        auth_service, "logger", structlog.get_logger(auth_service.__name__)
    )

    with structlog.testing.capture_logs() as logs:
        assert await run_sync_tick(worker) == 1

    assert await _display_name(db_session, user) is None
    courses = await db_session.scalars(
        select(UserCourse).where(UserCourse.user_id == user_id)
    )
    assert len(courses.all()) == 1
    run = await db_session.scalar(select(SyncRun))
    assert run.status == "succeeded"
    assert any(e["event"] == "auth.display_name.not_stored" for e in logs)
    assert "Student" not in repr(logs)


async def test_a_name_the_database_refuses_keeps_the_probe_result(
    db_session, prepared_engine, test_settings, monkeypatch
):
    user, _, _ = await _setup_user_job(db_session)
    user_id = user.id

    async def probe(*, token, assignment_ids, max_concurrency, on_fullname):
        on_fullname(_REFUSED_NAME)
        return {
            1: FetchedSubmission(
                moodle_assignment_id=1,
                is_submitted=True,
                submitted_at=datetime.now(UTC),
            )
        }

    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=10))]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)
    monkeypatch.setattr(worker.fetcher, "fetch_submission_status", probe, raising=False)

    await run_sync_tick(worker)

    assert await _display_name(db_session, user) is None
    assignment = await db_session.scalar(
        select(UserAssignment).where(UserAssignment.user_id == user_id)
    )
    assert assignment.provider_is_submitted is True
    run = await db_session.scalar(select(SyncRun))
    assert run.status == "succeeded"
