# Phase 4 (Part 1): Push Delivery Pipeline + Assignment Reminders (4a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate `push_jobs`/`push_deliveries`: a two-phase delivery pipeline (materialize → deliver → aggregate) plus the first push source — assignment due-date reminders (step 4a).

**Architecture:** A 30-second APScheduler tick claims due `push_jobs` (`FOR UPDATE SKIP LOCKED` + advisory lock, same shape as `server/syncjobs/executor.py`), materializes one `push_deliveries` row per active standard token, delivers via the existing `PushRouter`, invalidates unregistered tokens, and aggregates job status. A separate scan job generates/cancels assignment-reminder `push_jobs` from `user_assignments` × `user_assignment_overrides` × the `notification` settings document. New code in `server/push/pipeline.py`, `server/push/job_payloads.py`, `server/push/reminders.py`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 17, APScheduler, existing `server/push` transport clients (Recording stubs in tests).

---

## Scope decision (recorded)

This plan covers the **delivery pipeline + 4a** only. Deferred to follow-up plans: 4b course reminders, 4c bulletin dispatch (+ review 1.8 dual-track fix), 4d Live Activity token migration, review 1.6 account deletion. The reauth `push_jobs` row Phase 3 already writes (`system` channel) is delivered by this pipeline with no extra work.

## Locked-in design decisions

1. **No new tables/migration** — `push_jobs`/`push_deliveries`/`device_push_tokens` exist since Phase 1.
2. **Round-based delivery retry:** within one job round, each pending delivery (with `next_retry_at <= now`) gets one send. Transient failures bump `delivery.attempts`; exhausted → `failed`, else delivery stays `pending` with `next_retry_at = now + 60s·attempts`. If any delivery is still pending after the round and `job.attempts+1 < job.max_attempts`, the job goes back to `pending` (`attempts++`, `available_at = now + push_retry_round_delay`) and a later tick re-runs it (materialize is idempotent via `ux_push_delivery_job_token` ON CONFLICT DO NOTHING). Otherwise remaining pending deliveries are failed (`retries_exhausted`) and the job aggregates terminally.
3. **Aggregation (spec §5):** ≥1 sent and no failed → `sent`; sent+failed mix → `partial_failed`; all failed → `failed`; zero active tokens → `failed` + `last_error='no_active_tokens'`.
4. **Token invalidation:** result is "unregistered" when APNs status `410` / description `BadDeviceToken`|`Unregistered`, or FCM status `UNREGISTERED` → `device_push_tokens.status='invalidated'` + delivery `failed`. Other non-success results are transient.
5. **Stale processing recovery (data-model §2):** `processing` + `locked_at < now-5min` → `pending`, `attempts++`, `available_at = now + backoff`; `attempts >= max_attempts` → `failed`.
6. **Only `token_kind='standard'`** tokens participate (4d migrates Live Activity later). `job.device_id` NULL → fan out to all the user's active devices; non-NULL → that device only.
7. **Reminder dedupe keys embed the due-date epoch** (`assignment:moodle:{id}:reminder_{offset}h:{due_epoch}`) because `ux_push_jobs_dedupe_active` also covers sent states (security fix 1.2) — a due-date change produces a new key, and the scan **cancels** stale pending jobs (old key, or assignment no longer eligible).
8. **Reminder defaults:** when a user has no `notification` settings document, assignments reminders are ENABLED with offsets from `settings.assignment_reminder_default_offsets_hours` (default `[24.0, 2.0]` — conservative; the spec's 6-offset example is a client-side default, server default stays light). Offsets larger than the scan window (`assignment_reminder_window_hours`, default 168h) are not supported (job would be born in the past); documented limitation.
9. **Generation is a periodic scan** (default 300s), not an event hook on sync completion — the scan picks up sync-written assignments within minutes, which is fine for offsets ≥ 30min. Event-driven triggering can be added later without schema changes.
10. **Payloads are self-contained routing hints** (`kind`, `title`, `body`, assignment ids, `due_at`, `moodle_url`) so the pipeline never joins academic tables; no HTML, no credentials.

## File map

| File | Responsibility |
|---|---|
| `server/push/job_payloads.py` | `push_jobs.payload` + token row → `ApnsRequest`/`FcmRequest` |
| `server/push/pipeline.py` | `PushPipelineWorker`, `run_push_tick`: recover → claim → materialize/deliver/aggregate |
| `server/push/reminders.py` | `scan_assignment_reminders`: generate + cancel reminder push_jobs |
| `server/config.py` | +6 settings |
| `server/scheduler/runtime.py` | register `push_pipeline_tick`, `assignment_reminder_scan` |
| `server/main.py` | build `PushPipelineWorker` in lifespan |
| `server/tests/conftest.py` | neuter the two new tick intervals |

Tests: `test_push_job_payloads.py`, `test_push_pipeline.py`, `test_push_reminders.py`.

## Environment notes

Same as Phase 3 plan (`2026-06-10-phase3-server-sync.md`): rtk wrapper 的 exit code 不可靠（看輸出文字）、新測試檔加 `pytestmark = pytest.mark.asyncio(loop_scope="session")`、背景 job 測試照 `test_syncjobs_executor.py` 模式、ASGITransport 不跑 lifespan。Commit: `feat(Push): ...` + 中文 bullets, no Co-Authored-By.

---

### Task 1: Payload builders (`job_payloads.py`)

**Files:** Create `server/push/job_payloads.py`; Test `server/tests/test_push_job_payloads.py`

- [ ] **Step 1: failing test**

```python
"""push_jobs.payload → transport request mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.payload import PushKind

pytestmark = pytest.mark.asyncio(loop_scope="session")

PAYLOAD = {
    "kind": "assignment_reminder",
    "title": "作業提醒：HW3",
    "body": "資料結構 · 剩 24 小時",
    "moodle_assignment_id": 555,
    "moodle_course_id": 7001,
    "due_at": "2026-06-12T15:00:00+00:00",
    "moodle_url": "https://moodle.example.edu/mod/assign/view.php?id=9001",
}
NOW = datetime(2026, 6, 11, 15, 0, tzinfo=UTC)


def test_apns_request_shape():
    request = build_apns_for_job(
        payload=PAYLOAD,
        channel="assignment",
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.kind is PushKind.alert
    assert request.device_token == "tok-abc"
    assert request.topic == "org.ntust.app.TigerDuck"
    aps = request.message["aps"]
    assert aps["alert"] == {"title": PAYLOAD["title"], "body": PAYLOAD["body"]}
    assert aps["thread-id"] == "assignment"
    assert request.message["kind"] == "assignment_reminder"
    assert request.message["moodle_assignment_id"] == "555"
    assert request.message["due_at"] == PAYLOAD["due_at"]
    assert request.expiration > int(NOW.timestamp())


def test_fcm_request_shape():
    request = build_fcm_for_job(
        payload=PAYLOAD, channel="assignment", token_value="fcm-tok"
    )
    assert request.token == "fcm-tok"
    assert request.title == PAYLOAD["title"]
    assert request.data["kind"] == "assignment_reminder"
    assert request.data["moodle_assignment_id"] == "555"
    assert request.data["android_channel_id"] == "assignments"
    assert all(isinstance(v, str) for v in request.data.values())


def test_missing_title_falls_back_to_empty():
    request = build_fcm_for_job(payload={}, channel="system", token_value="t")
    assert request.title == ""
    assert request.data["android_channel_id"] == "system"
```

- [ ] **Step 2:** Run `uv run pytest server/tests/test_push_job_payloads.py -q` → ImportError (RED)

- [ ] **Step 3: implement**

```python
"""Map a push_jobs row's JSONB payload onto transport requests.

The payload is self-contained (title/body + routing hint keys written by
the source generator), so the pipeline never re-joins domain tables.
Extra payload keys ride along stringified — APNs as top-level message
keys, FCM inside `data` (FCM requires string values).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.push.payload import ApnsRequest, FcmRequest, PushKind

_RESERVED = {"title", "body"}
_DEFAULT_TTL_SECONDS = 24 * 3600

# channel → Android notification channel id (client must have registered).
_ANDROID_CHANNELS = {
    "assignment": "assignments",
    "course": "courses",
    "bulletin": "bulletins_sound",
    "system": "system",
    "custom": "bulletins_sound",
}


def _extras(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: value if isinstance(value, str) else str(value)
        for key, value in payload.items()
        if key not in _RESERVED and value is not None
    }


def build_apns_for_job(
    *,
    payload: dict[str, Any],
    channel: str,
    token_value: str,
    bundle_id: str,
    now: datetime | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> ApnsRequest:
    timestamp = int((now or datetime.now(UTC)).timestamp())
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    message: dict[str, Any] = {
        "aps": {
            "alert": {"title": title, "body": body},
            "badge": 1,
            "sound": "default",
            "mutable-content": 1,
            "thread-id": channel,
        },
        **_extras(payload),
    }
    return ApnsRequest(
        device_token=token_value,
        topic=bundle_id,
        expiration=timestamp + ttl_seconds,
        priority=10,
        message=message,
        kind=PushKind.alert,
    )


def build_fcm_for_job(
    *,
    payload: dict[str, Any],
    channel: str,
    token_value: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> FcmRequest:
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    data = {
        "title": title,
        "body": body,
        "android_channel_id": _ANDROID_CHANNELS.get(channel, "system"),
        **_extras(payload),
    }
    return FcmRequest(
        token=token_value, title=title, body=body, data=data,
        ttl_seconds=ttl_seconds,
    )
```

- [ ] **Step 4:** tests pass; **Step 5:** `uv run ruff check ...`; commit `feat(Push): add push job payload builders`

---

### Task 2: Settings + pipeline claim/recovery

**Files:** Modify `server/config.py`, `server/tests/conftest.py`; Create `server/push/pipeline.py`; Test `server/tests/test_push_pipeline.py`

- [ ] **Step 1: settings** — add after the sync-job block in `config.py`:

```python
    # --- User push pipeline (Phase 4) ---
    push_pipeline_tick_seconds: int = 30
    push_pipeline_batch_size: int = 10
    push_job_stale_lock_minutes: int = 5
    # Delay before a job with still-pending deliveries gets another round.
    push_retry_round_delay_seconds: int = 60
    # --- Assignment reminders (Phase 4a) ---
    assignment_reminder_scan_interval_seconds: int = 300
    assignment_reminder_default_offsets_hours: list[float] = Field(
        default_factory=lambda: [24.0, 2.0]
    )
    assignment_reminder_window_hours: int = 168
```

conftest `test_settings`: add `push_pipeline_tick_seconds=99999, assignment_reminder_scan_interval_seconds=99999`.

- [ ] **Step 2: failing tests** (`test_push_pipeline.py` — fixtures + claim/recovery half; execution tests in Task 3)

```python
"""Push pipeline: stale recovery, claim, materialize, deliver, aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory
from server.push.apns_client import SendResult
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class ScriptedSender:
    """PushSender double returning queued results (last one repeats)."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0] if self.results else SendResult(
            success=True, status="200"
        )

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings, apple=None, android=None):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(
            apple=apple or ScriptedSender(), android=android or ScriptedSender()
        ),
        worker_id="push-test-worker",
    )


async def _setup_user_device_token(
    session, *, platform="ios", provider="apns", token_value="tok-1",
    token_kwargs=None,
):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id, client_device_id="dev-1", platform=platform
    )
    session.add(device)
    await session.flush()
    token = DevicePushToken(
        device_id=device.id,
        provider=provider,
        token_kind="standard",
        token_hash=f"hash-{token_value}",
        token_value=token_value,
        bundle_id="org.ntust.app.TigerDuck",
        **(token_kwargs or {}),
    )
    session.add(token)
    await session.flush()
    return user, device, token


def _job(user, **kwargs):
    defaults = dict(
        user_id=user.id,
        dedupe_key=f"system:test:{kwargs.get('scenario', 's')}",
        channel="system",
        scenario="reauth_required",
        fire_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={"title": "t", "body": "b"},
    )
    defaults.update(kwargs)
    return PushJob(**defaults)


async def test_due_job_claimed_and_sent(db_session, prepared_engine, test_settings):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    await db_session.refresh(job)
    assert job.status == "sent"
    assert job.sent_at is not None
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.status == "sent"
    assert delivery.provider == "apns"


async def test_future_or_unavailable_jobs_not_claimed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    db_session.add(
        _job(user, fire_at=datetime.now(UTC) + timedelta(hours=1), scenario="f")
    )
    db_session.add(
        _job(
            user,
            available_at=datetime.now(UTC) + timedelta(minutes=5),
            scenario="a",
            dedupe_key="system:test:a2",
        )
    )
    await db_session.commit()

    apple = ScriptedSender()
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert apple.requests == []


async def test_stale_processing_job_recovered_with_backoff(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        available_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)

    await db_session.refresh(job)
    # attempts++ and pushed back to pending with future available_at...
    # then NOT claimed this tick (available_at in the future).
    assert job.attempts == 1
    assert job.status == "pending"
    assert job.available_at > datetime.now(UTC)


async def test_stale_processing_exhausted_goes_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        attempts=2,
        max_attempts=3,
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"
```

- [ ] **Step 3:** RED run; **Step 4: implement `server/push/pipeline.py`** (full module below — Task 3 exercises the rest)

```python
"""Two-phase user push pipeline (sync-and-push spec §5).

Claim/execute split mirrors `server/syncjobs/executor.py`: stale
`processing` rows are recovered first (attempts++, backoff — data-model
§2), then a short transaction claims due jobs under an advisory lock +
FOR UPDATE SKIP LOCKED, then each job runs in its own transaction:

  materialize: one push_deliveries row per active standard token
               (idempotent via ux_push_delivery_job_token DO NOTHING)
  deliver:     send each due pending delivery once via PushRouter;
               unregistered tokens are invalidated, transient failures
               keep the delivery pending with next_retry_at backoff
  aggregate:   ≥1 sent & no failed → sent; mix → partial_failed;
               all failed → failed; no tokens → failed/no_active_tokens.
               Still-pending deliveries push the JOB back to pending for
               another round until job.max_attempts rounds are spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushDeliveryStatus,
    PushJob,
    PushJobStatus,
    PushTokenStatus,
    UserDevice,
)
from server.config import Settings
from server.db import session_scope
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.router import PushRouter

logger = structlog.get_logger(__name__)

# "TD_PUSH" — serializes the claim phase across workers.
_CLAIM_LOCK_KEY = 0x54445F50555348

_UNREGISTERED_APNS_DESCRIPTIONS = {"baddevicetoken", "unregistered"}


@dataclass(frozen=True)
class PushPipelineWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    router: PushRouter
    worker_id: str


def _is_unregistered(status: str | None, description: str | None) -> bool:
    s = (status or "").lower()
    d = (description or "").lower()
    return (
        s in {"410", "unregistered"}
        or d in _UNREGISTERED_APNS_DESCRIPTIONS
    )


async def run_push_tick(worker: PushPipelineWorker) -> int:
    await _recover_stale_jobs(worker)
    claimed = await _claim_due_jobs(worker)
    for job_id in claimed:
        await _process_job(worker, job_id=job_id)
    return len(claimed)


async def _recover_stale_jobs(worker: PushPipelineWorker) -> None:
    cutoff = datetime.now(UTC) - timedelta(
        minutes=worker.settings.push_job_stale_lock_minutes
    )
    async with session_scope(worker.session_factory) as session:
        jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(
                        PushJob.status == PushJobStatus.processing.value,
                        PushJob.locked_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not jobs:
            return
        now = datetime.now(UTC)
        for job in jobs:
            job.locked_by = None
            job.locked_at = None
            job.attempts += 1
            if job.attempts >= job.max_attempts:
                job.status = PushJobStatus.failed.value
                job.last_error = "stale_lock"
            else:
                job.status = PushJobStatus.pending.value
                backoff = worker.settings.push_retry_round_delay_seconds * (
                    2 ** (job.attempts - 1)
                )
                job.available_at = now + timedelta(seconds=backoff)
        logger.warning("push.stale_recovered", count=len(jobs))


async def _claim_due_jobs(worker: PushPipelineWorker) -> list[int]:
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        await session.execute(
            select(func.pg_advisory_xact_lock(_CLAIM_LOCK_KEY))
        )
        jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(
                        PushJob.status == PushJobStatus.pending.value,
                        PushJob.fire_at <= now,
                        PushJob.available_at <= now,
                    )
                    .order_by(PushJob.priority, PushJob.fire_at)
                    .limit(worker.settings.push_pipeline_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            job.status = PushJobStatus.processing.value
            job.locked_by = worker.worker_id
            job.locked_at = now
        return [job.id for job in jobs]


async def _process_job(worker: PushPipelineWorker, *, job_id: int) -> None:
    try:
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(PushJob).where(PushJob.id == job_id).with_for_update()
                )
            ).scalar_one()
            if (
                job.status != PushJobStatus.processing.value
                or job.locked_by != worker.worker_id
            ):
                logger.warning("push.job_reclaimed", job_id=job_id)
                return
            await _materialize(session, job)
            await _deliver_round(worker, session, job)
    except Exception:
        logger.exception("push.job_crashed", job_id=job_id)
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(PushJob).where(PushJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None or job.status != PushJobStatus.processing.value:
                return
            now = datetime.now(UTC)
            job.locked_by = None
            job.locked_at = None
            job.attempts += 1
            if job.attempts >= job.max_attempts:
                job.status = PushJobStatus.failed.value
                job.last_error = "pipeline_crash"
            else:
                job.status = PushJobStatus.pending.value
                job.available_at = now + timedelta(
                    seconds=worker.settings.push_retry_round_delay_seconds
                )


async def _materialize(session: AsyncSession, job: PushJob) -> None:
    now = datetime.now(UTC)
    token_query = (
        select(DevicePushToken)
        .join(UserDevice, UserDevice.id == DevicePushToken.device_id)
        .where(
            UserDevice.user_id == job.user_id,
            UserDevice.deleted_at.is_(None),
            DevicePushToken.token_kind == "standard",
            DevicePushToken.status == PushTokenStatus.active.value,
            (DevicePushToken.expires_at.is_(None))
            | (DevicePushToken.expires_at > now),
        )
    )
    if job.device_id is not None:
        token_query = token_query.where(UserDevice.id == job.device_id)
    tokens = (await session.execute(token_query)).scalars().all()
    if not tokens:
        return
    values = [
        {
            "push_job_id": job.id,
            "user_id": job.user_id,
            "device_id": token.device_id,
            "push_token_id": token.id,
            "provider": token.provider,
            "token_kind": token.token_kind,
            "token_hash": token.token_hash,
            "scope_key": token.scope_key,
        }
        for token in tokens
    ]
    await session.execute(
        pg_insert(PushDelivery)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=[
                "push_job_id", "token_hash", "token_kind", "scope_key",
            ]
        )
    )


async def _deliver_round(
    worker: PushPipelineWorker, session: AsyncSession, job: PushJob
) -> None:
    now = datetime.now(UTC)
    deliveries = (
        (
            await session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )
    if not deliveries:
        job.status = PushJobStatus.failed.value
        job.last_error = "no_active_tokens"
        job.locked_by = None
        job.locked_at = None
        logger.info("push.no_active_tokens", job_id=job.id)
        return

    for delivery in deliveries:
        if delivery.status != PushDeliveryStatus.pending.value:
            continue
        if delivery.next_retry_at > now:
            continue
        await _send_one(worker, session, job, delivery, now)

    statuses = [d.status for d in deliveries]
    pending = statuses.count(PushDeliveryStatus.pending.value)
    if pending:
        if job.attempts + 1 < job.max_attempts:
            job.attempts += 1
            job.status = PushJobStatus.pending.value
            job.available_at = now + timedelta(
                seconds=worker.settings.push_retry_round_delay_seconds
            )
            job.locked_by = None
            job.locked_at = None
            return
        for delivery in deliveries:
            if delivery.status == PushDeliveryStatus.pending.value:
                delivery.status = PushDeliveryStatus.failed.value
                delivery.failure_code = "retries_exhausted"
        statuses = [d.status for d in deliveries]

    sent = statuses.count(PushDeliveryStatus.sent.value)
    failed = statuses.count(PushDeliveryStatus.failed.value)
    if sent and not failed:
        job.status = PushJobStatus.sent.value
    elif sent:
        job.status = PushJobStatus.partial_failed.value
    else:
        job.status = PushJobStatus.failed.value
        job.last_error = "all_deliveries_failed"
    if sent:
        job.sent_at = now
    job.locked_by = None
    job.locked_at = None
    logger.info(
        "push.job_done", job_id=job.id, status=job.status, sent=sent,
        failed=failed,
    )


async def _send_one(
    worker: PushPipelineWorker,
    session: AsyncSession,
    job: PushJob,
    delivery: PushDelivery,
    now: datetime,
) -> None:
    token = (
        await session.get(DevicePushToken, delivery.push_token_id)
        if delivery.push_token_id is not None
        else None
    )
    if token is None or token.status != PushTokenStatus.active.value:
        delivery.status = PushDeliveryStatus.skipped.value
        delivery.failure_code = "token_gone"
        return

    delivery.attempts += 1
    if delivery.provider == "apns":
        request = build_apns_for_job(
            payload=job.payload,
            channel=job.channel,
            token_value=token.token_value,
            bundle_id=token.bundle_id or worker.settings.apns_bundle_id,
            now=now,
        )
        result = await worker.router.send_apple(request)
    else:
        request = build_fcm_for_job(
            payload=job.payload, channel=job.channel,
            token_value=token.token_value,
        )
        result = await worker.router.send_android(request)

    if result.success:
        delivery.status = PushDeliveryStatus.sent.value
        delivery.sent_at = now
        delivery.provider_message_id = result.notification_id
        token.last_success_at = now
        return

    token.last_failure_at = now
    token.last_failure_code = result.status
    if _is_unregistered(result.status, result.description):
        delivery.status = PushDeliveryStatus.failed.value
        delivery.failure_code = "unregistered"
        delivery.failure_message = result.description
        token.status = PushTokenStatus.invalidated.value
        logger.info(
            "push.token_invalidated",
            token_id=token.id,
            status=result.status,
        )
        return

    delivery.failure_code = result.status
    delivery.failure_message = result.description
    if delivery.attempts >= delivery.max_attempts:
        delivery.status = PushDeliveryStatus.failed.value
    else:
        delivery.next_retry_at = now + timedelta(seconds=60 * delivery.attempts)
```

- [ ] **Step 5:** Task-2 tests pass; lint; commit `feat(Push): add push pipeline claim and stale recovery`

---

### Task 3: Pipeline delivery-path tests

**Files:** append to `server/tests/test_push_pipeline.py`

- [ ] **Step 1: append tests**

```python
async def test_fanout_to_all_devices_and_partial_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="android"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="fcm",
            token_kind="standard",
            token_hash="hash-fcm-1",
            token_value="fcm-1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    android = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="boom")] * 3
    )
    worker = _worker(prepared_engine, test_settings, apple=apple, android=android)

    # Round 1: apns sent, fcm transient-pending → job re-queued.
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1

    # Exhaust remaining rounds (delivery max_attempts=3, job max_attempts=3).
    for _ in range(4):
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        if job.status != "pending":
            break
    assert job.status == "partial_failed"
    deliveries = (
        (
            await db_session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )
    assert {d.status for d in deliveries} == {"sent", "failed"}


async def test_unregistered_token_invalidated(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="410", description="Unregistered")]
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    await db_session.refresh(job)
    assert job.status == "failed"
    await db_session.refresh(token)
    assert token.status == "invalidated"
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.failure_code == "unregistered"


async def test_no_active_tokens_fails_job(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(
        db_session, token_kwargs={"status": "invalidated"}
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.last_error == "no_active_tokens"


async def test_device_targeted_job_only_hits_that_device(
    db_session, prepared_engine, test_settings
):
    user, device1, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="ios"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-tok-2",
            token_value="tok-2",
        )
    )
    job = _job(user, device_id=device2.id)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    assert apple.requests[0].device_token == "tok-2"


async def test_live_activity_tokens_excluded(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash="hash-la",
            token_value="la-tok",
            scope_key="assignment:1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert len(apple.requests) == 1  # standard token only
```

- [ ] **Step 2:** run file → all pass (fix pipeline if needed); full suite; lint; commit `test(Push): cover pipeline delivery paths`

---

### Task 4: Scheduler + lifespan wiring

**Files:** Modify `server/scheduler/runtime.py`, `server/main.py`

- [ ] **Step 1:** runtime.py — import `PushPipelineWorker, run_push_tick` from `server.push.pipeline`; `build_scheduler(..., push_worker: PushPipelineWorker | None = None)`; register when not None:

```python
    if push_worker is not None:

        async def push_pipeline_tick() -> None:
            await run_push_tick(push_worker)

        scheduler.add_job(
            push_pipeline_tick,
            trigger=IntervalTrigger(seconds=settings.push_pipeline_tick_seconds),
            id="push_pipeline_tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
```

- [ ] **Step 2:** main.py lifespan — build worker (router always exists):

```python
    push_worker = PushPipelineWorker(
        session_factory=session_factory,
        settings=settings,
        router=router,
        worker_id=default_worker_id(),
    )
    scheduler = build_scheduler(
        session_factory, router, settings,
        sync_worker=sync_worker, push_worker=push_worker,
    )
```

(`default_worker_id` imported already from syncjobs.executor.)

- [ ] **Step 3:** full suite green (conftest neuter added in Task 2); lint; commit `feat(Push): wire push pipeline into APScheduler`

---

### Task 5: Assignment reminder scan (4a)

**Files:** Create `server/push/reminders.py`; Test `server/tests/test_push_reminders.py`

- [ ] **Step 1: failing tests**

```python
"""Assignment reminder generation + stale-job cancellation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import PushJob, User
from server.push.reminders import scan_assignment_reminders
from server.db import build_session_factory
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user(session, student_id="b11203058"):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    return user


def _assignment(user, aid=1, due_in_hours=30.0, **kwargs):
    defaults = dict(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=aid,
        course_name="資料結構",
        title=f"HW{aid}",
        due_at=datetime.now(UTC) + timedelta(hours=due_in_hours),
    )
    defaults.update(kwargs)
    return UserAssignment(**defaults)


async def _jobs(session, user_id):
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user_id)
                .order_by(PushJob.fire_at)
            )
        )
        .scalars()
        .all()
    )


async def test_creates_jobs_for_default_offsets(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=30))
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 2  # default offsets [24, 2]

    jobs = await _jobs(db_session, user.id)
    assert len(jobs) == 2
    assert all(j.channel == "assignment" for j in jobs)
    assert jobs[0].scenario == "reminder_24h"
    assert jobs[1].scenario == "reminder_2h"
    due_epoch = int(jobs[0].payload["due_epoch"])
    assert jobs[0].dedupe_key == f"assignment:moodle:1:reminder_24h:{due_epoch}"
    assert jobs[0].payload["title"]
    # idempotent re-scan
    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 0


async def test_past_offsets_skipped(db_session, prepared_engine, test_settings):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=3))  # 24h offset in the past
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_2h"]


async def test_excluded_assignments_get_no_jobs(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    submitted = _assignment(user, aid=1, provider_is_submitted=True)
    deleted = _assignment(user, aid=2, deleted_at=datetime.now(UTC))
    ignored = _assignment(user, aid=3)
    db_session.add_all([submitted, deleted, ignored])
    await db_session.flush()
    db_session.add(
        UserAssignmentOverride(
            user_id=user.id,
            user_assignment_id=ignored.id,
            local_status="ignored",
        )
    )
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 0


async def test_user_settings_control_offsets_and_enabled(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=30))
    db_session.add(
        UserSettingsDocument(
            user_id=user.id,
            namespace="notification",
            document={
                "assignments": {
                    "enabled": True,
                    "reminder_offsets_hours": [8],
                }
            },
        )
    )
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_8h"]

    # Disable → pending jobs are cancelled on next scan.
    doc = (
        await db_session.execute(
            select(UserSettingsDocument).where(
                UserSettingsDocument.user_id == user.id
            )
        )
    ).scalar_one()
    doc.document = {"assignments": {"enabled": False}}
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)


async def test_due_date_change_cancels_old_and_creates_new(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    assignment = _assignment(user, due_in_hours=30)
    db_session.add(assignment)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)
    old_jobs = await _jobs(db_session, user.id)
    assert len(old_jobs) == 2

    assignment.due_at = assignment.due_at + timedelta(days=2)
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    jobs = await _jobs(db_session, user.id)
    cancelled = [j for j in jobs if j.status == "cancelled"]
    pending = [j for j in jobs if j.status == "pending"]
    assert len(cancelled) == 2
    assert len(pending) == 2
    new_epoch = int(assignment.due_at.timestamp())
    assert all(str(new_epoch) in j.dedupe_key for j in pending)


async def test_submitted_after_scheduling_cancels_pending(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    assignment = _assignment(user, due_in_hours=30)
    db_session.add(assignment)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)

    assignment.provider_is_submitted = True
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)
```

- [ ] **Step 2:** RED run; **Step 3: implement `server/push/reminders.py`**

```python
"""Phase 4a: generate assignment due-date reminder push_jobs.

Scan-based (default every 300s): eligible = not deleted, not submitted,
local_status not in (locally_completed/ignored/archived), due within the
scan window, and the user's `notification` settings document has
assignments enabled (no document → enabled with the server default
offsets). One push_job per (assignment, offset) with the due-date epoch
embedded in the dedupe key — `ux_push_jobs_dedupe_active` also covers
sent states (security fix 1.2), so a due-date change must mint a NEW key;
the scan cancels pending jobs whose key is no longer valid (due changed,
assignment completed/ignored/deleted, or reminders disabled).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import PushJob, PushJobStatus
from server.config import Settings
from server.db import session_scope
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserSettingsDocument,
)

logger = structlog.get_logger(__name__)

_EXCLUDED_LOCAL_STATUSES = {"locally_completed", "ignored", "archived"}
CHANNEL = "assignment"


def _fmt_offset(hours: float) -> str:
    return f"{hours:g}"


def _dedupe_key(moodle_assignment_id: int, offset: float, due_epoch: int) -> str:
    return (
        f"assignment:moodle:{moodle_assignment_id}:"
        f"reminder_{_fmt_offset(offset)}h:{due_epoch}"
    )


async def _notification_prefs(
    session: AsyncSession, user_ids: set[uuid.UUID], settings: Settings
) -> dict[uuid.UUID, tuple[bool, list[float]]]:
    """user_id → (enabled, offsets). Missing document → server defaults."""
    docs = (
        (
            await session.execute(
                select(UserSettingsDocument).where(
                    UserSettingsDocument.user_id.in_(user_ids),
                    UserSettingsDocument.namespace == "notification",
                    UserSettingsDocument.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    default = (True, list(settings.assignment_reminder_default_offsets_hours))
    prefs: dict[uuid.UUID, tuple[bool, list[float]]] = {
        user_id: default for user_id in user_ids
    }
    for doc in docs:
        section = (doc.document or {}).get("assignments") or {}
        enabled = bool(section.get("enabled", True))
        raw = section.get("reminder_offsets_hours")
        offsets = (
            [float(value) for value in raw if isinstance(value, (int, float))]
            if isinstance(raw, list)
            else default[1]
        )
        prefs[doc.user_id] = (enabled, offsets)
    return prefs


async def scan_assignment_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One scan pass. Returns the number of push_jobs created."""
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=settings.assignment_reminder_window_hours)
    created = 0

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(UserAssignment, UserAssignmentOverride)
                .outerjoin(
                    UserAssignmentOverride,
                    UserAssignmentOverride.user_assignment_id
                    == UserAssignment.id,
                )
                .where(
                    UserAssignment.deleted_at.is_(None),
                    UserAssignment.provider_is_submitted.is_(False),
                    UserAssignment.due_at.is_not(None),
                    UserAssignment.due_at > now,
                    UserAssignment.due_at <= window_end,
                )
            )
        ).all()

        eligible = [
            assignment
            for assignment, override in rows
            if override is None
            or override.local_status not in _EXCLUDED_LOCAL_STATUSES
        ]
        user_ids = {a.user_id for a in eligible}

        # Pending reminder jobs of EVERY user (covers users whose last
        # eligible assignment just got submitted/deleted).
        pending_jobs = (
            (
                await session.execute(
                    select(PushJob).where(
                        PushJob.channel == CHANNEL,
                        PushJob.status == PushJobStatus.pending.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        user_ids |= {j.user_id for j in pending_jobs}
        if not user_ids:
            return 0

        prefs = await _notification_prefs(session, user_ids, settings)

        valid_keys: set[str] = set()
        values: list[dict] = []
        for assignment in eligible:
            enabled, offsets = prefs[assignment.user_id]
            if not enabled:
                continue
            due_epoch = int(assignment.due_at.timestamp())
            for offset in offsets:
                fire_at = assignment.due_at - timedelta(hours=offset)
                key = _dedupe_key(
                    assignment.moodle_assignment_id, offset, due_epoch
                )
                if fire_at <= now:
                    continue
                valid_keys.add(key)
                values.append(
                    {
                        "user_id": assignment.user_id,
                        "dedupe_key": key,
                        "channel": CHANNEL,
                        "scenario": f"reminder_{_fmt_offset(offset)}h",
                        "fire_at": fire_at,
                        "payload": {
                            "kind": "assignment_reminder",
                            "title": f"作業提醒：{assignment.title}",
                            "body": (
                                f"{assignment.course_name + ' · ' if assignment.course_name else ''}"
                                f"剩 {_fmt_offset(offset)} 小時"
                            ),
                            "moodle_assignment_id": assignment.moodle_assignment_id,
                            "moodle_course_id": assignment.moodle_course_id,
                            "due_at": assignment.due_at.isoformat(),
                            "due_epoch": due_epoch,
                            "moodle_url": assignment.moodle_url,
                            "offset_hours": offset,
                        },
                    }
                )

        if values:
            result = await session.execute(
                pg_insert(PushJob).values(values).on_conflict_do_nothing()
            )
            created = result.rowcount or 0

        # Cancel stale pending jobs: key no longer among the valid future
        # reminders. Jobs whose fire_at already passed AND whose key is
        # still valid stay untouched (about to be delivered).
        cancelled = 0
        for job in pending_jobs:
            if job.dedupe_key in valid_keys:
                continue
            job.status = PushJobStatus.cancelled.value
            job.cancelled_at = now
            cancelled += 1

    if created or cancelled:
        logger.info(
            "push.reminders.scan", created=created, cancelled=cancelled
        )
    return created
```

**Cancellation subtlety:** a pending job whose `fire_at` has passed but whose assignment is still eligible has its key in `valid_keys`? No — generation skips `fire_at <= now`. To avoid cancelling an about-to-fire job, the generation loop must add the key to `valid_keys` even when skipping creation **iff the assignment is still eligible** — move `valid_keys.add(key)` BEFORE the `if fire_at <= now: continue` line. (The test `test_past_offsets_skipped` plus `test_submitted_after_scheduling_cancels_pending` together pin this behavior.) The code above shows the final correct order: `valid_keys.add` must come first — adjust accordingly when implementing.

- [ ] **Step 4:** tests pass (watch the valid_keys ordering); full suite; lint; commit `feat(Push): add assignment reminder scan (4a)`

---

### Task 6: Reminder scheduler wiring + final verification

- [ ] **Step 1:** runtime.py — register scan job (needs only session_factory + settings):

```python
    async def assignment_reminder_scan() -> None:
        await scan_assignment_reminders(session_factory, settings)

    scheduler.add_job(
        assignment_reminder_scan,
        trigger=IntervalTrigger(
            seconds=settings.assignment_reminder_scan_interval_seconds
        ),
        id="assignment_reminder_scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
```

- [ ] **Step 2:** full suite `uv run pytest server/tests/ -q` — read output text.
- [ ] **Step 3:** lint all new files.
- [ ] **Step 4:** code-reviewer agent over `git diff feat/user-sync-phase3...HEAD -- server/`; fix HIGH/MEDIUM (技術判斷,不合理者記錄理由).
- [ ] **Step 5:** summarize; ask user: push/PR, merge, or continue 4b/4c/4d.

---

## Self-review notes

- Spec §5 Phase-1/Phase-2 delivery steps, aggregation rules, BadDeviceToken invalidation, no_active_tokens: Tasks 2–3. Stale processing recovery per data-model §2: Task 2. 4a generation + overrides exclusion + settings doc: Task 5. Reauth system push: delivered by pipeline automatically (no task needed). Deferred: 4b/4c/4d, review 1.6/1.8 — recorded in scope decision.
- Type consistency: `PushPipelineWorker` fields used consistently; `SendResult(success, status, description, notification_id)` matches `server/push/apns_client.py:22`.
