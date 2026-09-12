"""Executor: stale-lock recovery, claiming, global cap, execution paths."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import select, text

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
    UserDevice,
)
from server.config import Settings
from server.db import build_session_factory
from server.logging_setup import configure
from server.sync.models import UserAssignment, UserChangeLog, UserSyncState
from server.syncjobs import executor as executor_module
from server.syncjobs.executor import SyncWorker, run_sync_tick
from server.syncjobs.models import SyncJob, SyncPolicy, SyncRun
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
)
from server.syncjobs.policies import ensure_default_policies

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
    def __init__(self, results=None, errors=()):
        self.results = results if results is not None else []
        self.errors = list(errors)

    async def fetch_courses(self, *, token):
        if self.errors:
            raise self.errors.pop(0)
        return self.results


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
):
    cipher = _cipher()
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
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
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
        **(job_kwargs or {}),
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


async def test_network_failure_backs_off_then_terminal_after_max_attempts(
    db_session, prepared_engine, test_settings
):
    user, _, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(
        errors=[
            MoodleUnreachable("net"),
            MoodleUnreachable("net"),
            MoodleUnreachable("net"),
        ]
    )
    worker = _worker(prepared_engine, test_settings, fetcher=fetcher)

    await run_sync_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1
    assert job.last_error.startswith("sync_failed")
    assert job.run_after > datetime.now(UTC)  # backoff in the future

    # Force-due and run twice more → terminal failed at max_attempts=3.
    for _ in range(2):
        job.run_after = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_sync_tick(worker)
        await db_session.refresh(job)
    assert job.status == "failed"
    assert job.attempts == 3


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

    async def fake_probe(*, token, assignment_ids, max_concurrency):
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

    async def exploding_probe(*, token, assignment_ids, max_concurrency):
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

    async def dead_token_probe(*, token, assignment_ids, max_concurrency):
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

    async def rate_limited_probe(*, token, assignment_ids, max_concurrency):
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


async def test_submission_probe_db_error_does_not_roll_back_assignment_sync(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A genuine database error while refreshing submission status (not a
    Moodle call) must not undo the assignment list sync, which has already
    committed by then, must not fail the run, and must not be recorded as a
    probe failure."""
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

    await run_sync_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_success_at is not None
    assert job.last_error is None

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
    assert run.status == "succeeded"


async def test_submission_status_settings_are_threaded_through(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A non-default window/concurrency setting must actually reach
    select_assignment_ids and the fetcher, not just exist in config."""
    await _setup_user_job(db_session)
    seen: dict = {}

    async def recording_probe(*, token, assignment_ids, max_concurrency):
        seen["assignment_ids"] = list(assignment_ids)
        seen["max_concurrency"] = max_concurrency
        return {}

    # Due in 60h: inside a widened 72h window, outside the *default* 48h
    # window -- proves window_hours actually comes from settings rather
    # than being hardcoded to the default.
    fetcher = StubFetcher(
        results=[_fa(1, due_at=datetime.now(UTC) + timedelta(hours=60))]
    )
    settings = test_settings.model_copy(
        update={
            "submission_status_window_hours": 72,
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

    async def escalating_probe(*, token, assignment_ids, max_concurrency):
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

    async def observing_probe(*, token, assignment_ids, max_concurrency):
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
        *, token, assignment_ids, max_concurrency
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
        configure(Settings(env="production", log_level="INFO"))
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

    async def leaky_probe(*, token, assignment_ids, max_concurrency):
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
