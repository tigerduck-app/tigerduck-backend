# Phase 3: Server-Side Academic Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend scheduler periodically fetches Moodle assignments with stored credentials, upserts `user_assignments`, and feeds the change log; users get pull-to-refresh and admins get policy control.

**Architecture:** Three new tables (`sync_policies` / `sync_jobs` / `sync_runs`) drive a 30-second APScheduler tick. A short claim transaction marks due jobs `running` (`FOR UPDATE SKIP LOCKED`, batch ≤ 5, global running cap); each job then executes in its own transaction: decrypt credentials → fetch via cached Moodle token (re-obtain via NTUST password under the single-attempt iron rule) → upsert `user_assignments` → append `user_change_log` under the per-user sync-state lock. New code lives in a new `server/syncjobs/` package; routes in `server/routes/sync_jobs.py`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 17, Alembic, APScheduler, httpx (+MockTransport in tests), pytest-asyncio.

---

## Locked-in design decisions

1. **Package layout:** `server/syncjobs/` — `models.py`, `policies.py`, `provisioning.py`, `credentials.py`, `moodle_client.py`, `assignments.py`, `executor.py`. Routes in `server/routes/sync_jobs.py` (user router + admin router).
2. **Job creation at login** (handoff item 5, chosen over backfill): `service.login` upserts `sync_jobs` for every job type in `HANDLED_JOB_TYPES` (Phase 3: only `moodle_assignments`). The **executor filters by policy `enabled` + active window at claim time**, so an admin toggling a policy takes effect for all users instantly — no backfill needed. Re-login resets `disabled` jobs back to `pending` (credentials were just re-stored).
3. **Policy seeding twice, both idempotent:** the Alembic migration seeds the four rows (production path), and `ensure_default_policies` (`ON CONFLICT DO NOTHING`) runs in lifespan so `create_all`-based test DBs and fresh envs are covered.
4. **Submission status is NOT fetched in Phase 3.** `mod_assign_get_assignments` doesn't include per-user submission state and fetching it is one extra call per assignment. Server sync never touches `provider_is_submitted` / `provider_submitted_at` (client-uploaded values survive). Open item for Phase 4 (reminders need it).
5. **Credential iron rule (security review 1.4):** auth-class SSO failure (`SsoAuthFailed`) → `CredentialInvalid` → `external_accounts.credential_status='invalid'`, ALL user sync_jobs `disabled`, one `push_jobs` row (channel `system`, dedupe `system:account:{external_account_id}:reauth_required`) — **never retried**. Network-class failure (`SsoUnavailable`) → normal backoff retry; the password was never evaluated so no attempt is consumed. On SSO success the blob is re-encrypted with `password_verified=true` + fresh `token_cache`.
6. **Pull-to-refresh cooldown** lives in `sync_jobs.cursor` JSONB (`manual_requested_at`) — DB-backed, multi-instance safe. Cooldown only burns when a new high-priority run is actually queued.
7. **Stale-lock recovery** (running > 10 min): reset to `pending`, clear lock, mark the orphaned `sync_runs` row `failed` (`error='stale_lock_recovered'`). No `attempts++` (spec lifecycle doesn't increment on recovery).
8. **`ntust_courses` / `calendar` / `grades` fetchers are NOT implemented** (policies exist; `ntust_courses`/`grades` seeded disabled). The assignments+courses co-fetch optimization (spec §4) is **deferred** — there is no course fetcher to share a session with. `calendar` is seeded enabled per spec but no per-user jobs are created for it (not in `HANDLED_JOB_TYPES`), so the executor never sees it.
9. **Empty fetch = authoritative mirror:** assignments absent from a successful fetch are soft-deleted (+`delete` changelog); they resurrect (clear `deleted_at`, `upsert` changelog) if they reappear. A transient empty response self-heals on the next run.
10. **Phase-4 reauth push:** we only INSERT the `push_jobs` row; delivery pipeline activates in Phase 4 (handoff item 9, decision recorded here).

## File map

| File | Responsibility |
|---|---|
| `server/syncjobs/__init__.py` | empty package marker |
| `server/syncjobs/models.py` | `SyncPolicy`, `SyncJob`, `SyncRun` ORM + StrEnums |
| `server/syncjobs/policies.py` | `DEFAULT_POLICIES`, `ensure_default_policies` |
| `server/syncjobs/provisioning.py` | `HANDLED_JOB_TYPES`, `ensure_sync_jobs` (login hook) |
| `server/syncjobs/moodle_client.py` | `AssignmentFetcher`/`TokenObtainer` Protocols + Http impls + errors |
| `server/syncjobs/credentials.py` | decrypt blob, token refresh w/ iron rule, `mark_credentials_invalid` |
| `server/syncjobs/assignments.py` | `apply_fetched_assignments` upsert + changelog |
| `server/syncjobs/executor.py` | `SyncWorker`, tick: recover → claim → execute |
| `server/routes/sync_jobs.py` | `POST /v3/sync-jobs/run-now`, admin `GET/PATCH /v3/admin/sync-policies` |
| `server/models.py` | +1 registration import |
| `server/config.py` | +8 settings |
| `server/main.py` | lifespan: seed policies, build worker; mount routers |
| `server/scheduler/runtime.py` | register `sync_jobs_tick` |
| `server/auth/service.py` | login calls `ensure_sync_jobs` |
| `server/migrations/versions/<new>_phase3_sync_jobs.py` | 3 tables + seed |

Test files: `test_syncjobs_models.py`, `test_syncjobs_policies.py`, `test_moodle_fetch_client.py`, `test_syncjobs_credentials.py`, `test_syncjobs_assignments.py`, `test_syncjobs_executor.py`, `test_syncjobs_provisioning.py`, `test_sync_jobs_api.py`, `test_admin_sync_policies.py`.

## Environment notes (read before every task)

- Run tests: `uv run pytest server/tests/ -q` — **the rtk wrapper makes exit codes unreliable; read the pytest output text, never trust `&&` chains.**
- Every new test file starts with `pytestmark = pytest.mark.asyncio(loop_scope="session")`.
- Lint only new files: `uv run ruff check server/syncjobs/ server/routes/sync_jobs.py server/tests/test_syncjobs_*.py server/tests/test_sync_jobs_api.py server/tests/test_admin_sync_policies.py server/tests/test_moodle_fetch_client.py`
- Test DB container `tigerduck-test-pg` must be running (`docker ps`); if missing: `docker run -d --name tigerduck-test-pg -e POSTGRES_USER=tigerduck -e POSTGRES_PASSWORD=tigerduck -e POSTGRES_DB=tigerduck -p 5432:5432 postgres:17-alpine`
- Commit format: `feat(SyncJobs): short description` + 中文 bullet body, NO Co-Authored-By.

---

### Task 1: ORM models for sync_policies / sync_jobs / sync_runs

**Files:**
- Create: `server/syncjobs/__init__.py` (empty)
- Create: `server/syncjobs/models.py`
- Modify: `server/models.py` (add registration import after line 40)
- Test: `server/tests/test_syncjobs_models.py`

- [x] **Step 1: Write the failing test**

```python
"""Roundtrip + constraint tests for the Phase-3 sync infrastructure tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.auth.models import ExternalAccount, User
from server.syncjobs.models import (
    SyncJob,
    SyncJobStatus,
    SyncJobType,
    SyncPolicy,
    SyncRun,
    SyncRunStatus,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user_account(session) -> tuple[User, ExternalAccount]:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()
    return user, account


async def test_sync_policy_roundtrip_defaults(db_session):
    policy = SyncPolicy(
        job_type=SyncJobType.moodle_assignments.value,
        default_interval_seconds=28800,
    )
    db_session.add(policy)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(
                SyncPolicy.job_type == "moodle_assignments"
            )
        )
    ).scalar_one()
    assert row.enabled is True
    assert row.priority == 100
    assert row.max_attempts == 3
    assert row.active_from is None and row.active_until is None


async def test_sync_policy_rejects_unknown_job_type(db_session):
    db_session.add(SyncPolicy(job_type="bogus", default_interval_seconds=60))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_policy_job_type_unique(db_session):
    db_session.add(
        SyncPolicy(job_type="calendar", default_interval_seconds=604800)
    )
    await db_session.commit()
    db_session.add(
        SyncPolicy(job_type="calendar", default_interval_seconds=60)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_job_roundtrip_and_unique_per_user_type(db_session):
    user, account = await _make_user_account(db_session)
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type=SyncJobType.moodle_assignments.value,
    )
    db_session.add(job)
    await db_session.commit()

    assert job.status == SyncJobStatus.pending.value
    assert job.priority == 100
    assert job.attempts == 0
    assert job.cursor == {}
    assert job.run_after is not None

    db_session.add(
        SyncJob(
            user_id=user.id,
            external_account_id=account.id,
            job_type=SyncJobType.moodle_assignments.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_job_rejects_bad_status(db_session):
    user, account = await _make_user_account(db_session)
    db_session.add(
        SyncJob(
            user_id=user.id,
            external_account_id=account.id,
            job_type="moodle_assignments",
            status="exploded",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_sync_run_roundtrip(db_session):
    user, account = await _make_user_account(db_session)
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
    )
    db_session.add(job)
    await db_session.flush()

    run = SyncRun(sync_job_id=job.id, user_id=user.id)
    db_session.add(run)
    await db_session.commit()

    assert run.status == SyncRunStatus.running.value
    assert run.started_at is not None
    assert run.metadata_json == {}

    run.status = SyncRunStatus.succeeded.value
    run.finished_at = datetime.now(UTC) + timedelta(seconds=1)
    run.fetched_count = 12
    run.changed_count = 3
    await db_session.commit()
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_syncjobs_models.py -q`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'server.syncjobs'`

- [x] **Step 3: Write the models**

Create empty `server/syncjobs/__init__.py`. Create `server/syncjobs/models.py`:

```python
"""ORM models for the Phase-3 server-side sync tables (data-model spec §6).

Conventions match `server/auth/models.py` / `server/sync/models.py`:
StrEnum + String columns with CHECK constraints (no PG ENUM), TIMESTAMPTZ,
partial indexes via `postgresql_where`.

`sync_runs.metadata` is a reserved name on SQLAlchemy declarative models,
so the attribute is `metadata_json` mapped onto the `metadata` column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class SyncJobType(StrEnum):
    ntust_courses = "ntust_courses"
    moodle_assignments = "moodle_assignments"
    calendar = "calendar"
    grades = "grades"


class SyncJobStatus(StrEnum):
    pending = "pending"
    running = "running"
    failed = "failed"
    disabled = "disabled"


class SyncRunStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


_JOB_TYPE_CHECK = (
    "job_type IN ('ntust_courses', 'moodle_assignments', 'calendar', 'grades')"
)


class SyncPolicy(Base):
    """Global per-job-type scheduling configuration, managed via the admin
    dashboard. One row per job type."""

    __tablename__ = "sync_policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(32), unique=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    default_interval_seconds: Mapped[int] = mapped_column(Integer)
    active_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(_JOB_TYPE_CHECK, name="chk_sync_policy_job_type"),
        CheckConstraint(
            "default_interval_seconds > 0", name="chk_sync_policy_interval"
        ),
        CheckConstraint("max_attempts > 0", name="chk_sync_policy_attempts"),
    )


class SyncJob(Base):
    """One recurring server-side sync job per (user, job_type). On success
    the row returns to `pending` with `run_after = now + interval`."""

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    external_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("external_accounts.id", ondelete="CASCADE")
    )
    job_type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    status: Mapped[str] = mapped_column(
        String(16), default=SyncJobStatus.pending.value, server_default="pending"
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "job_type"),
        CheckConstraint(_JOB_TYPE_CHECK, name="chk_sync_job_type"),
        CheckConstraint(
            "status IN ('pending', 'running', 'failed', 'disabled')",
            name="chk_sync_job_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0", name="chk_sync_job_attempts"
        ),
        Index(
            "idx_sync_jobs_due",
            "status",
            "run_after",
            "priority",
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )


class SyncRun(Base):
    """Execution history: one row per attempt of one sync_job."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sync_job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_jobs.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=SyncRunStatus.running.value, server_default="running"
    )
    fetched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="chk_sync_run_status",
        ),
        Index("idx_sync_runs_job", "sync_job_id", sa.text("started_at DESC")),
        Index("idx_sync_runs_user_recent", "user_id", sa.text("started_at DESC")),
    )
```

Then in `server/models.py`, directly below `import server.sync.models  # noqa: F401, E402` add:

```python
import server.syncjobs.models  # noqa: F401, E402
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_syncjobs_models.py -q`
Expected: all PASS (read output text).

- [x] **Step 5: Lint + commit**

Run: `uv run ruff check server/syncjobs/ server/tests/test_syncjobs_models.py`

```bash
git add server/syncjobs/ server/models.py server/tests/test_syncjobs_models.py
git commit -m "feat(SyncJobs): add sync_policies/sync_jobs/sync_runs ORM models

- 依 data-model spec §6 新增三張 server-side sync 表的 ORM
- 沿用 StrEnum + CHECK constraint 慣例，pending 部分索引 idx_sync_jobs_due
- sync_runs.metadata 因 SQLAlchemy 保留字以 metadata_json 屬性映射"
```

---

### Task 2: Default policy seeding helper + lifespan wiring

**Files:**
- Create: `server/syncjobs/policies.py`
- Modify: `server/main.py` (lifespan)
- Test: `server/tests/test_syncjobs_policies.py`

- [x] **Step 1: Write the failing test**

```python
"""Default sync_policies seeding (idempotent)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.syncjobs.models import SyncPolicy
from server.syncjobs.policies import ensure_default_policies

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXPECTED = {
    "moodle_assignments": (28800, True),
    "ntust_courses": (28800, False),
    "calendar": (604800, True),
    "grades": (28800, False),
}


async def test_seeds_four_default_policies(db_session):
    await ensure_default_policies(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(SyncPolicy))).scalars().all()
    assert {
        r.job_type: (r.default_interval_seconds, r.enabled) for r in rows
    } == EXPECTED


async def test_seeding_is_idempotent_and_keeps_admin_edits(db_session):
    await ensure_default_policies(db_session)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(SyncPolicy.job_type == "ntust_courses")
        )
    ).scalar_one()
    row.enabled = True
    row.default_interval_seconds = 3600
    await db_session.commit()

    await ensure_default_policies(db_session)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(SyncPolicy).where(SyncPolicy.job_type == "ntust_courses")
        )
    ).scalar_one()
    assert row.enabled is True
    assert row.default_interval_seconds == 3600
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_syncjobs_policies.py -q`
Expected: FAIL with `ModuleNotFoundError` / `ImportError` on `server.syncjobs.policies`

- [x] **Step 3: Implement `server/syncjobs/policies.py`**

```python
"""Default sync_policies seed values (migration plan Phase 3).

Seeded both by the Alembic migration (production) and at app startup via
`ensure_default_policies` (covers create_all-based test DBs and fresh
environments). ON CONFLICT DO NOTHING — admin edits always win.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.syncjobs.models import SyncJobType, SyncPolicy

DEFAULT_POLICIES: tuple[dict, ...] = (
    {
        "job_type": SyncJobType.moodle_assignments.value,
        "default_interval_seconds": 28800,
        "enabled": True,
    },
    {
        "job_type": SyncJobType.ntust_courses.value,
        "default_interval_seconds": 28800,
        "enabled": False,
    },
    {
        "job_type": SyncJobType.calendar.value,
        "default_interval_seconds": 604800,
        "enabled": True,
    },
    {
        "job_type": SyncJobType.grades.value,
        "default_interval_seconds": 28800,
        "enabled": False,
    },
)


async def ensure_default_policies(session: AsyncSession) -> None:
    await session.execute(
        pg_insert(SyncPolicy)
        .values(list(DEFAULT_POLICIES))
        .on_conflict_do_nothing(index_elements=["job_type"])
    )
```

- [x] **Step 4: Wire into lifespan**

In `server/main.py` lifespan, after `session_factory = build_session_factory(engine)` (line ~110) add:

```python
from server.db import session_scope  # add to existing server.db import line
from server.syncjobs.policies import ensure_default_policies  # top of file

    async with session_scope(session_factory) as seed_session:
        await ensure_default_policies(seed_session)
```

- [x] **Step 5: Run tests**

Run: `uv run pytest server/tests/test_syncjobs_policies.py -q` → PASS
Run: `uv run pytest server/tests/ -q` → full suite still green (lifespan change touches every e2e test).

- [x] **Step 6: Lint + commit**

```bash
git add server/syncjobs/policies.py server/main.py server/tests/test_syncjobs_policies.py
git commit -m "feat(SyncJobs): seed default sync policies at startup

- ensure_default_policies 以 ON CONFLICT DO NOTHING 植入四種 job_type 預設值
- lifespan 啟動時執行，create_all 測試環境與新環境皆有 policy 可用
- admin 修改過的值不會被啟動覆寫"
```

---

### Task 3: Alembic migration + seed

**Files:**
- Create: `server/migrations/versions/<autogen>_phase3_sync_jobs.py`

- [x] **Step 1: Create scratch DB and bring it to current head**

```bash
docker exec tigerduck-test-pg psql -U tigerduck -c "DROP DATABASE IF EXISTS tigerduck_mig"
docker exec tigerduck-test-pg psql -U tigerduck -c "CREATE DATABASE tigerduck_mig"
TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic upgrade head
```

Expected: upgrades through `b8d4f0a2c3e9` without error.

- [x] **Step 2: Autogenerate**

```bash
TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic revision --autogenerate -m "phase3 sync jobs"
```

- [x] **Step 3: Clean the generated file**

In the new file under `server/migrations/versions/`:
1. **Delete the `ix_devices_class_enabled` drift** — remove the `op.drop_index(...ix_devices_class_enabled...)` line in `upgrade()` AND the matching `op.create_index(...)` line in `downgrade()`.
2. Verify it creates exactly `sync_policies`, `sync_jobs`, `sync_runs` with the CHECK constraints and indexes from Task 1 (autogenerate picks them up from the models).
3. Append the seed at the END of `upgrade()`:

```python
    op.execute(
        """
        INSERT INTO sync_policies (job_type, enabled, default_interval_seconds)
        VALUES
            ('moodle_assignments', true, 28800),
            ('ntust_courses', false, 28800),
            ('calendar', true, 604800),
            ('grades', false, 28800)
        ON CONFLICT (job_type) DO NOTHING
        """
    )
```

(No seed-delete needed in `downgrade()` — it drops the whole table.)

- [x] **Step 4: Verify up/down/up roundtrip**

```bash
TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic upgrade head
TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic downgrade b8d4f0a2c3e9
TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic upgrade head
docker exec tigerduck-test-pg psql -U tigerduck -d tigerduck_mig -c "SELECT job_type, enabled, default_interval_seconds FROM sync_policies ORDER BY job_type"
```

Expected: 4 seeded rows; no errors on any step.

- [x] **Step 5: Drop scratch DB + commit**

```bash
docker exec tigerduck-test-pg psql -U tigerduck -c "DROP DATABASE IF EXISTS tigerduck_mig"
git add server/migrations/versions/
git commit -m "feat(SyncJobs): add phase3 migration for sync job tables

- 建立 sync_policies / sync_jobs / sync_runs 三張表
- upgrade 內植入四種 job_type 的預設 policy（ON CONFLICT DO NOTHING）
- 已驗證 up/down/up roundtrip，並移除 autogenerate 誤抓的 ix_devices_class_enabled drift"
```

---

### Task 4: Moodle fetch client + token obtainer

**Files:**
- Create: `server/syncjobs/moodle_client.py`
- Test: `server/tests/test_moodle_fetch_client.py`

- [x] **Step 1: Write the failing test**

```python
"""HttpAssignmentFetcher / HttpTokenObtainer against httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from server.syncjobs.moodle_client import (
    HttpAssignmentFetcher,
    HttpTokenObtainer,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoAuthFailed,
    SsoUnavailable,
)

BASE = "https://moodle.example.edu"

ASSIGNMENTS_BODY = {
    "courses": [
        {
            "id": 7001,
            "fullname": "資料結構",
            "shortname": "CS2006301",
            "assignments": [
                {
                    "id": 555,
                    "cmid": 9001,
                    "course": 7001,
                    "name": "HW3",
                    "duedate": 1765400400,
                    "allowsubmissionsfromdate": 1764800400,
                    "cutoffdate": 0,
                    "intro": "<p>do it</p>",
                },
                {
                    "id": 556,
                    "cmid": 9002,
                    "course": 7001,
                    "name": "HW4",
                    "duedate": 0,
                    "allowsubmissionsfromdate": 0,
                    "cutoffdate": 0,
                    "intro": "",
                },
            ],
        }
    ]
}


def _fetcher(handler) -> HttpAssignmentFetcher:
    return HttpAssignmentFetcher(
        base_url=BASE,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


def _obtainer(handler) -> HttpTokenObtainer:
    return HttpTokenObtainer(
        base_url=BASE,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


async def test_fetch_assignments_parses_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["wsfunction"] == "mod_assign_get_assignments"
        assert request.url.params["wstoken"] == "tok-1"
        return httpx.Response(200, json=ASSIGNMENTS_BODY)

    fetched = await _fetcher(handler).fetch_assignments(token="tok-1")
    assert len(fetched) == 2
    hw3 = fetched[0]
    assert hw3.moodle_course_id == 7001
    assert hw3.moodle_assignment_id == 555
    assert hw3.course_name == "資料結構"
    assert hw3.title == "HW3"
    assert hw3.due_at == datetime.fromtimestamp(1765400400, tz=UTC)
    assert hw3.cutoff_at is None  # unix 0 → None
    assert hw3.moodle_url == f"{BASE}/mod/assign/view.php?id=9001"
    assert hw3.intro_html == "<p>do it</p>"
    hw4 = fetched[1]
    assert hw4.due_at is None and hw4.allow_from_at is None


async def test_fetch_assignments_invalid_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            },
        )

    with pytest.raises(MoodleTokenInvalid):
        await _fetcher(handler).fetch_assignments(token="dead")


async def test_fetch_assignments_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(MoodleRateLimited):
        await _fetcher(handler).fetch_assignments(token="tok")


async def test_fetch_assignments_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(MoodleUnreachable):
        await _fetcher(handler).fetch_assignments(token="tok")


async def test_obtain_token_success():
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(
            pair.split("=", 1)
            for pair in request.read().decode().split("&")
        )
        assert body["username"] == "b11203058"
        assert body["service"] == "moodle_mobile_app"
        return httpx.Response(
            200, json={"token": "new-tok", "privatetoken": "new-priv"}
        )

    got = await _obtainer(handler).obtain_token(
        username="b11203058", password="pw"
    )
    assert got.token == "new-tok"
    assert got.private_token == "new-priv"


async def test_obtain_token_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "Invalid login", "errorcode": "invalidlogin"},
        )

    with pytest.raises(SsoAuthFailed):
        await _obtainer(handler).obtain_token(username="u", password="bad")


async def test_obtain_token_network_failure_is_not_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(SsoUnavailable):
        await _obtainer(handler).obtain_token(username="u", password="pw")


async def test_obtain_token_server_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    with pytest.raises(SsoUnavailable):
        await _obtainer(handler).obtain_token(username="u", password="pw")
```

(Note: no `pytestmark` loop-scope needed for pure-httpx tests, but keep the file consistent — add `pytestmark = pytest.mark.asyncio(loop_scope="session")` anyway.)

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_moodle_fetch_client.py -q`
Expected: FAIL with `ModuleNotFoundError` on `server.syncjobs.moodle_client`

- [x] **Step 3: Implement `server/syncjobs/moodle_client.py`**

```python
"""Moodle webservice clients used by the server-side sync worker.

Same pattern as `server/auth/moodle.py`: Protocol interfaces, Http
implementations with an injectable `httpx` transport so tests stay
offline via MockTransport.

Error taxonomy is the load-bearing part — the executor's retry policy
hangs off it (security review 1.4):

* `MoodleTokenInvalid`  → cached token died; try ONE SSO re-obtain.
* `SsoAuthFailed`       → auth-class: credentials rejected. NEVER retried.
* `SsoUnavailable`      → network-class: SSO never evaluated the password,
                          safe to retry with backoff.
* `MoodleRateLimited`   → school API pushback; retry with backoff.
* `MoodleUnreachable`   → transient transport/parse error; retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

_WS_PATH = "/webservice/rest/server.php"
_TOKEN_PATH = "/login/token.php"
_TOKEN_SERVICE = "moodle_mobile_app"


class MoodleClientError(Exception):
    """Base for all Moodle/SSO client failures."""


class MoodleTokenInvalid(MoodleClientError):
    pass


class MoodleRateLimited(MoodleClientError):
    pass


class MoodleUnreachable(MoodleClientError):
    pass


class SsoAuthFailed(MoodleClientError):
    pass


class SsoUnavailable(MoodleClientError):
    pass


@dataclass(frozen=True)
class FetchedAssignment:
    moodle_course_id: int
    moodle_assignment_id: int
    course_name: str | None
    title: str
    due_at: datetime | None
    cutoff_at: datetime | None
    allow_from_at: datetime | None
    moodle_url: str | None
    intro_html: str | None


@dataclass(frozen=True)
class ObtainedToken:
    token: str
    private_token: str | None


class AssignmentFetcher(Protocol):
    async def fetch_assignments(self, *, token: str) -> list[FetchedAssignment]: ...


class TokenObtainer(Protocol):
    async def obtain_token(
        self, *, username: str, password: str
    ) -> ObtainedToken: ...


def _ts(value: object) -> datetime | None:
    """Moodle uses unix seconds with 0 meaning 'unset'."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


class HttpAssignmentFetcher:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch_assignments(self, *, token: str) -> list[FetchedAssignment]:
        params = {
            "wstoken": token,
            "wsfunction": "mod_assign_get_assignments",
            "moodlewsrestformat": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{self._base_url}{_WS_PATH}", params=params
                )
        except httpx.HTTPError as exc:
            # Never log the token.
            logger.warning("syncjobs.moodle.unreachable", error=str(exc)[:200])
            raise MoodleUnreachable(str(exc)[:200]) from exc

        if response.status_code == 429:
            raise MoodleRateLimited("http_429")
        try:
            body = response.json()
        except ValueError as exc:
            raise MoodleUnreachable("invalid_json") from exc
        if not isinstance(body, dict) or response.status_code >= 400:
            raise MoodleUnreachable(f"http_{response.status_code}")
        if "exception" in body:
            # Moodle reports webservice errors as 200 + exception payload.
            errorcode = str(body.get("errorcode", ""))
            if errorcode == "invalidtoken":
                raise MoodleTokenInvalid(errorcode)
            raise MoodleUnreachable(errorcode or "moodle_exception")

        fetched: list[FetchedAssignment] = []
        for course in body.get("courses", []):
            course_id = course.get("id")
            if not isinstance(course_id, int):
                continue
            course_name = course.get("fullname") or None
            for item in course.get("assignments", []):
                assignment_id = item.get("id")
                if not isinstance(assignment_id, int):
                    continue
                cmid = item.get("cmid")
                fetched.append(
                    FetchedAssignment(
                        moodle_course_id=course_id,
                        moodle_assignment_id=assignment_id,
                        course_name=course_name,
                        title=str(item.get("name") or ""),
                        due_at=_ts(item.get("duedate")),
                        cutoff_at=_ts(item.get("cutoffdate")),
                        allow_from_at=_ts(item.get("allowsubmissionsfromdate")),
                        moodle_url=(
                            f"{self._base_url}/mod/assign/view.php?id={cmid}"
                            if isinstance(cmid, int)
                            else None
                        ),
                        intro_html=item.get("intro") or None,
                    )
                )
        return fetched


class HttpTokenObtainer:
    """Re-obtains a Moodle webservice token from a username/password via
    `/login/token.php`. Called by the sync worker only when the cached
    token has expired — and at most once per run (iron rule lives in
    `server/syncjobs/credentials.py`)."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def obtain_token(
        self, *, username: str, password: str
    ) -> ObtainedToken:
        data = {
            "username": username,
            "password": password,
            "service": _TOKEN_SERVICE,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}{_TOKEN_PATH}", data=data
                )
        except httpx.HTTPError as exc:
            logger.warning("syncjobs.sso.unreachable", error=str(exc)[:200])
            raise SsoUnavailable(str(exc)[:200]) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise SsoUnavailable(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SsoUnavailable("invalid_json") from exc
        if not isinstance(body, dict):
            raise SsoUnavailable("invalid_body")

        token = body.get("token")
        if isinstance(token, str) and token:
            return ObtainedToken(
                token=token, private_token=body.get("privatetoken") or None
            )
        errorcode = str(body.get("errorcode", ""))
        if errorcode == "invalidlogin":
            logger.warning("syncjobs.sso.auth_failed", username=username)
            raise SsoAuthFailed(errorcode)
        raise SsoUnavailable(errorcode or f"http_{response.status_code}")
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_moodle_fetch_client.py -q` → PASS

- [x] **Step 5: Lint + commit**

```bash
git add server/syncjobs/moodle_client.py server/tests/test_moodle_fetch_client.py
git commit -m "feat(SyncJobs): add Moodle assignment fetcher and SSO token obtainer

- Protocol + injectable transport 模式，測試走 MockTransport 不打真實 API
- 錯誤分類為 retry 策略核心：SsoAuthFailed（認證類，絕不重試）與
  SsoUnavailable / MoodleUnreachable / MoodleRateLimited（網路類，backoff 重試）
- unix 0 時間戳解析為 None，moodle_url 由 cmid 組出"
```

---

### Task 5: Credential helpers (decrypt / token refresh iron rule / invalidate)

**Files:**
- Create: `server/syncjobs/credentials.py`
- Test: `server/tests/test_syncjobs_credentials.py`

- [x] **Step 1: Write the failing test**

```python
"""Credential blob handling for the sync worker, incl. the iron rule."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
)
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential_blob,
    mark_credentials_invalid,
    refresh_moodle_token,
)
from server.syncjobs.models import SyncJob
from server.syncjobs.moodle_client import (
    ObtainedToken,
    SsoAuthFailed,
    SsoUnavailable,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cipher() -> CredentialCipher:
    return CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )


async def _setup(session, cipher, *, password_verified=False):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()
    blob = cipher.encrypt(
        {
            "ntust_password": "secret-pw",
            "password_verified": password_verified,
            "token_cache": {"moodle_token": "old-tok"},
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
    )
    session.add(job)
    await session.commit()
    return user, account, job


class StaticObtainer:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = 0

    async def obtain_token(self, *, username, password):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


async def test_load_credential_blob_roundtrip(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    loaded_account, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    assert loaded_account.id == account.id
    assert blob["ntust_password"] == "secret-pw"
    assert blob["token_cache"]["moodle_token"] == "old-tok"


async def test_load_rejects_non_active_credential_status(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    account.credential_status = "invalid"
    await db_session.commit()
    with pytest.raises(CredentialInvalid):
        await load_credential_blob(
            db_session, cipher, external_account_id=account.id
        )


async def test_refresh_token_success_marks_password_verified(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher, password_verified=False)
    obtainer = StaticObtainer(
        result=ObtainedToken(token="fresh-tok", private_token="fresh-priv")
    )
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )

    token = await refresh_moodle_token(
        db_session, cipher, obtainer, account=account, blob=blob
    )
    await db_session.commit()
    assert token == "fresh-tok"
    assert obtainer.calls == 1

    _, blob2 = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    assert blob2["password_verified"] is True
    assert blob2["token_cache"]["moodle_token"] == "fresh-tok"
    assert blob2["token_cache"]["moodle_private_token"] == "fresh-priv"
    assert blob2["ntust_password"] == "secret-pw"


async def test_refresh_token_auth_failure_raises_credential_invalid(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    obtainer = StaticObtainer(error=SsoAuthFailed("invalidlogin"))
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    with pytest.raises(CredentialInvalid):
        await refresh_moodle_token(
            db_session, cipher, obtainer, account=account, blob=blob
        )
    assert obtainer.calls == 1  # exactly one SSO attempt, never more


async def test_refresh_token_network_failure_propagates(db_session):
    cipher = _cipher()
    _, account, _ = await _setup(db_session, cipher)
    obtainer = StaticObtainer(error=SsoUnavailable("timeout"))
    _, blob = await load_credential_blob(
        db_session, cipher, external_account_id=account.id
    )
    with pytest.raises(SsoUnavailable):
        await refresh_moodle_token(
            db_session, cipher, obtainer, account=account, blob=blob
        )


async def test_mark_credentials_invalid_disables_and_notifies(db_session):
    cipher = _cipher()
    user, account, job = await _setup(db_session, cipher)
    await mark_credentials_invalid(
        db_session,
        account=account,
        user_id=user.id,
        error="credential_invalid",
    )
    await db_session.commit()

    assert account.credential_status == "invalid"
    assert account.last_auth_error == "credential_invalid"
    refreshed_job = await db_session.get(SyncJob, job.id)
    await db_session.refresh(refreshed_job)
    assert refreshed_job.status == "disabled"

    push = (
        await db_session.execute(
            select(PushJob).where(PushJob.user_id == user.id)
        )
    ).scalar_one()
    assert push.channel == "system"
    assert push.scenario == "reauth_required"
    assert push.dedupe_key == f"system:account:{account.id}:reauth_required"

    # Idempotent: second invalidation does not duplicate the push job.
    await mark_credentials_invalid(
        db_session, account=account, user_id=user.id, error="credential_invalid"
    )
    await db_session.commit()
    pushes = (
        (
            await db_session.execute(
                select(PushJob).where(PushJob.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(pushes) == 1
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_syncjobs_credentials.py -q`
Expected: FAIL with `ModuleNotFoundError` on `server.syncjobs.credentials`

- [x] **Step 3: Implement `server/syncjobs/credentials.py`**

```python
"""Credential blob access for the sync worker + the password iron rule.

Security review 1.4 — THE iron rule: the stored NTUST password may be
presented to SSO at most once per run, and an auth-class rejection
(`SsoAuthFailed`) immediately invalidates the credential and disables all
of the user's sync jobs. It is NEVER retried — repeated wrong-password
attempts could lock the student's school account. Network-class failures
(`SsoUnavailable`) propagate to the executor's normal backoff path; SSO
never evaluated the password, so no attempt was consumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.crypto import (
    CredentialCipher,
    CredentialCipherError,
    EncryptedBlob,
    build_credential_aad,
)
from server.auth.models import (
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
)
from server.syncjobs.models import SyncJob, SyncJobStatus
from server.syncjobs.moodle_client import SsoAuthFailed, TokenObtainer

logger = structlog.get_logger(__name__)

REAUTH_SCENARIO = "reauth_required"


class CredentialInvalid(Exception):
    """Credentials unusable — sync for this account must be disabled."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def load_credential_blob(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    external_account_id: int,
) -> tuple[ExternalAccount, dict]:
    account = await session.get(ExternalAccount, external_account_id)
    if account is None:
        raise CredentialInvalid("account_missing")
    if account.credential_status != CredentialStatus.active.value:
        raise CredentialInvalid(f"credential_status_{account.credential_status}")
    credential = await session.get(ExternalAccountCredential, external_account_id)
    if credential is None:
        raise CredentialInvalid("credential_missing")
    try:
        blob = cipher.decrypt(
            EncryptedBlob(
                key_id=credential.encryption_key_id,
                nonce=credential.nonce,
                ciphertext=credential.ciphertext,
                aad=credential.aad,
            )
        )
    except CredentialCipherError as exc:
        logger.error(
            "syncjobs.credentials.decrypt_failed",
            external_account_id=external_account_id,
        )
        raise CredentialInvalid("decrypt_failed") from exc
    credential.last_used_at = datetime.now(UTC)
    return account, blob


async def refresh_moodle_token(
    session: AsyncSession,
    cipher: CredentialCipher,
    obtainer: TokenObtainer,
    *,
    account: ExternalAccount,
    blob: dict,
) -> str:
    """One SSO attempt to mint a fresh Moodle token. See module docstring
    for the retry semantics. On success the blob is re-encrypted with
    `password_verified=true` and the new token cache."""
    password = blob.get("ntust_password")
    if not isinstance(password, str) or not password:
        raise CredentialInvalid("password_missing")

    try:
        obtained = await obtainer.obtain_token(
            username=account.external_user_id, password=password
        )
    except SsoAuthFailed as exc:
        raise CredentialInvalid("sso_rejected") from exc
    # SsoUnavailable intentionally propagates — network-class, retriable.

    now = datetime.now(UTC)
    new_blob = {
        **blob,
        "password_verified": True,
        "token_cache": {
            "moodle_token": obtained.token,
            "moodle_private_token": obtained.private_token,
            "obtained_at": now.isoformat(),
        },
    }
    encrypted = cipher.encrypt(
        new_blob, build_credential_aad(account.id, account.provider)
    )
    credential = await session.get(ExternalAccountCredential, account.id)
    if credential is None:  # account row vanished mid-run
        raise CredentialInvalid("credential_missing")
    credential.encryption_key_id = encrypted.key_id
    credential.ciphertext = encrypted.ciphertext
    credential.nonce = encrypted.nonce
    credential.aad = encrypted.aad
    credential.rotated_at = now
    account.last_auth_success_at = now
    account.last_auth_error = None
    logger.info("syncjobs.credentials.token_refreshed", account_id=account.id)
    return obtained.token


async def mark_credentials_invalid(
    session: AsyncSession,
    *,
    account: ExternalAccount,
    user_id: uuid.UUID,
    error: str,
) -> None:
    """Credential failure handling (sync-and-push spec §4): flag the
    account, disable every sync job for the user, and queue ONE system
    push (delivery pipeline activates in Phase 4; the dedupe index makes
    re-invalidation idempotent)."""
    now = datetime.now(UTC)
    account.credential_status = CredentialStatus.invalid.value
    account.last_auth_failure_at = now
    account.last_auth_error = error

    await session.execute(
        update(SyncJob)
        .where(SyncJob.user_id == user_id)
        .values(
            status=SyncJobStatus.disabled.value,
            locked_by=None,
            locked_at=None,
            last_failure_at=now,
            last_error=error,
        )
    )

    await session.execute(
        pg_insert(PushJob)
        .values(
            user_id=user_id,
            dedupe_key=f"system:account:{account.id}:{REAUTH_SCENARIO}",
            channel="system",
            scenario=REAUTH_SCENARIO,
            fire_at=now,
            payload={"reason": "credential_invalid", "provider": account.provider},
        )
        .on_conflict_do_nothing()
    )
    logger.warning(
        "syncjobs.credentials.invalidated",
        account_id=account.id,
        user_id=str(user_id),
        error=error,
    )
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_syncjobs_credentials.py -q` → PASS

- [x] **Step 5: Lint + commit**

```bash
git add server/syncjobs/credentials.py server/tests/test_syncjobs_credentials.py
git commit -m "feat(SyncJobs): add credential helpers with single-attempt SSO rule

- load_credential_blob 解密並驗證 credential_status
- refresh_moodle_token 鐵律：SSO 認證失敗一次即 CredentialInvalid，絕不重試；
  網路錯誤原樣拋出走 backoff（密碼未被驗證、不算消耗）
- mark_credentials_invalid 停用該 user 全部 sync_jobs 並寫入
  system reauth push_job（dedupe 冪等，Phase 4 啟用發送）"
```

---

### Task 6: Assignment upsert + changelog

**Files:**
- Create: `server/syncjobs/assignments.py`
- Test: `server/tests/test_syncjobs_assignments.py`

- [x] **Step 1: Write the failing test**

```python
"""apply_fetched_assignments: insert / update / soft-delete / resurrect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import User
from server.sync.models import UserAssignment, UserChangeLog, UserCourse
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.moodle_client import FetchedAssignment

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime.now(UTC)


def _fa(assignment_id: int, **kwargs) -> FetchedAssignment:
    defaults = dict(
        moodle_course_id=7001,
        moodle_assignment_id=assignment_id,
        course_name="資料結構",
        title=f"HW{assignment_id}",
        due_at=NOW + timedelta(days=7),
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=f"https://moodle.example.edu/mod/assign/view.php?id={assignment_id}",
        intro_html=None,
    )
    defaults.update(kwargs)
    return FetchedAssignment(**defaults)


async def _make_user(session) -> User:
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    return user


async def _changelog(session, user_id):
    return (
        (
            await session.execute(
                select(UserChangeLog)
                .where(UserChangeLog.user_id == user_id)
                .order_by(UserChangeLog.revision)
            )
        )
        .scalars()
        .all()
    )


async def test_inserts_new_assignments_with_changelog(db_session):
    user = await _make_user(db_session)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    assert stats.fetched_count == 2
    assert stats.changed_count == 2
    rows = (
        (
            await db_session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.moodle_assignment_id for r in rows} == {1, 2}
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert all(e.entity_type == "assignment" for e in entries)
    assert all(e.operation == "upsert" for e in entries)
    assert all(e.device_id is None for e in entries)


async def test_unchanged_assignment_writes_no_changelog(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    later = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=later
    )
    await db_session.commit()

    assert stats.changed_count == 0
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 1
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.last_seen_at == later  # metadata still refreshed
    assert row.fetched_at == later


async def test_changed_field_updates_and_logs_field_names(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    new_due = NOW + timedelta(days=14)
    stats = await apply_fetched_assignments(
        db_session,
        user_id=user.id,
        fetched=[_fa(1, due_at=new_due, title="HW1-renamed")],
        now=NOW + timedelta(hours=8),
    )
    await db_session.commit()

    assert stats.changed_count == 1
    entries = await _changelog(db_session, user.id)
    assert len(entries) == 2
    assert set(entries[-1].payload["fields"]) == {"due_at", "title"}
    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.title == "HW1-renamed"
    assert row.due_at == new_due


async def test_absent_assignment_soft_deleted_then_resurrected(db_session):
    user = await _make_user(db_session)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=NOW
    )
    await db_session.commit()

    t2 = NOW + timedelta(hours=8)
    stats = await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=t2
    )
    await db_session.commit()
    assert stats.changed_count == 1
    gone = (
        await db_session.execute(
            select(UserAssignment).where(
                UserAssignment.user_id == user.id,
                UserAssignment.moodle_assignment_id == 2,
            )
        )
    ).scalar_one()
    assert gone.deleted_at is not None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "delete"
    assert entries[-1].entity_id == str(gone.id)

    t3 = NOW + timedelta(hours=16)
    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1), _fa(2)], now=t3
    )
    await db_session.commit()
    await db_session.refresh(gone)
    assert gone.deleted_at is None
    entries = await _changelog(db_session, user.id)
    assert entries[-1].operation == "upsert"


async def test_links_assignment_to_course_by_moodle_id(db_session):
    user = await _make_user(db_session)
    course = UserCourse(
        user_id=user.id,
        semester="1142",
        course_key="CS2006301",
        course_no="CS2006301",
        course_name="資料結構",
        moodle_id="7001",
    )
    db_session.add(course)
    await db_session.flush()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1)], now=NOW
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UserAssignment).where(UserAssignment.user_id == user.id)
        )
    ).scalar_one()
    assert row.user_course_id == course.id
    assert row.course_no == "CS2006301"


async def test_never_touches_provider_submission_fields(db_session):
    user = await _make_user(db_session)
    existing = UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=1,
        title="HW1",
        provider_is_submitted=True,
        provider_submitted_at=NOW - timedelta(days=1),
    )
    db_session.add(existing)
    await db_session.commit()

    await apply_fetched_assignments(
        db_session, user_id=user.id, fetched=[_fa(1, title="HW1")], now=NOW
    )
    await db_session.commit()
    await db_session.refresh(existing)
    assert existing.provider_is_submitted is True
    assert existing.provider_submitted_at is not None
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_syncjobs_assignments.py -q`
Expected: FAIL with `ModuleNotFoundError` on `server.syncjobs.assignments`

- [x] **Step 3: Implement `server/syncjobs/assignments.py`**

```python
"""Mirror fetched Moodle assignments into user_assignments + changelog.

A successful fetch is treated as the authoritative snapshot of the
account's assignments: new rows insert, changed provider fields update,
rows absent from the fetch soft-delete, previously-deleted rows that
reappear resurrect. Submission state (`provider_is_submitted` /
`provider_submitted_at` / grading fields) is NOT fetched in Phase 3 and
is never written here — client-uploaded values survive.

Changelog appends run under the per-user sync-state lock
(`lock_sync_state`), acquired once per run; payloads carry routing hints
(changed field names) only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.changelog import append_change, lock_sync_state
from server.sync.models import ChangeEntityType, UserAssignment, UserCourse
from server.syncjobs.moodle_client import FetchedAssignment

logger = structlog.get_logger(__name__)

# Provider-owned fields mirrored 1:1 from the fetch result.
_PROVIDER_FIELDS = (
    "course_name",
    "title",
    "due_at",
    "cutoff_at",
    "allow_from_at",
    "moodle_url",
    "intro_html",
)


@dataclass(frozen=True)
class AssignmentSyncStats:
    fetched_count: int
    changed_count: int


async def apply_fetched_assignments(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    fetched: list[FetchedAssignment],
    now: datetime,
) -> AssignmentSyncStats:
    locked_state = await lock_sync_state(session, user_id)

    async def log(entity_id: str, operation: str, hint: dict | None = None):
        await append_change(
            session,
            user_id=user_id,
            entity_type=ChangeEntityType.assignment.value,
            entity_id=entity_id,
            operation=operation,
            payload=hint,
            device_id=None,
            locked_state=locked_state,
        )

    existing_rows = (
        (
            await session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    existing = {
        (r.moodle_course_id, r.moodle_assignment_id): r for r in existing_rows
    }

    courses = (
        (
            await session.execute(
                select(UserCourse).where(
                    UserCourse.user_id == user_id,
                    UserCourse.moodle_id.is_not(None),
                    UserCourse.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    course_by_moodle_id = {c.moodle_id: c for c in courses}

    changed = 0
    seen: set[tuple[int, int]] = set()

    for item in fetched:
        key = (item.moodle_course_id, item.moodle_assignment_id)
        seen.add(key)
        linked_course = course_by_moodle_id.get(str(item.moodle_course_id))
        row = existing.get(key)

        if row is None:
            row = UserAssignment(
                user_id=user_id,
                user_course_id=linked_course.id if linked_course else None,
                moodle_course_id=item.moodle_course_id,
                moodle_assignment_id=item.moodle_assignment_id,
                course_no=linked_course.course_no if linked_course else None,
                course_name=item.course_name,
                title=item.title,
                due_at=item.due_at,
                cutoff_at=item.cutoff_at,
                allow_from_at=item.allow_from_at,
                moodle_url=item.moodle_url,
                intro_html=item.intro_html,
                fetched_at=now,
                last_seen_at=now,
            )
            session.add(row)
            await session.flush()
            existing[key] = row
            changed += 1
            await log(str(row.id), "upsert", {"fields": list(_PROVIDER_FIELDS)})
            continue

        fields: list[str] = []
        for field_name in _PROVIDER_FIELDS:
            new_value = getattr(item, field_name)
            if getattr(row, field_name) != new_value:
                setattr(row, field_name, new_value)
                fields.append(field_name)
        if linked_course is not None and row.user_course_id != linked_course.id:
            row.user_course_id = linked_course.id
            row.course_no = linked_course.course_no
            fields.append("user_course_id")
        if row.deleted_at is not None:
            row.deleted_at = None
            fields.append("deleted_at")
        row.fetched_at = now
        row.last_seen_at = now
        if fields:
            changed += 1
            await log(str(row.id), "upsert", {"fields": fields})

    for key, row in existing.items():
        if key in seen or row.deleted_at is not None:
            continue
        row.deleted_at = now
        changed += 1
        await log(str(row.id), "delete")

    logger.info(
        "syncjobs.assignments.applied",
        user_id=str(user_id),
        fetched=len(fetched),
        changed=changed,
    )
    return AssignmentSyncStats(fetched_count=len(fetched), changed_count=changed)
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_syncjobs_assignments.py -q` → PASS

- [x] **Step 5: Lint + commit**

```bash
git add server/syncjobs/assignments.py server/tests/test_syncjobs_assignments.py
git commit -m "feat(SyncJobs): mirror fetched assignments into user_assignments

- 成功 fetch 視為權威快照：新增/欄位更新/缺席軟刪/重現復活
- changelog 走 lock_sync_state 單次鎖 + append_change(locked_state=)，
  payload 只帶變更欄位名
- 不寫 provider_is_submitted 等繳交欄位（Phase 3 不抓 submission status）
- moodle_id 對應 user_courses 建立 user_course_id 連結"
```

---

### Task 7: Settings + executor claim / stale-lock recovery / global cap

**Files:**
- Modify: `server/config.py`
- Create: `server/syncjobs/executor.py` (claim + recovery half)
- Test: `server/tests/test_syncjobs_executor.py` (first half)

- [x] **Step 1: Add settings**

In `server/config.py` after the "Sync change log retention" block insert:

```python
    # --- Server-side sync jobs (Phase 3) ---
    sync_job_tick_seconds: int = 30
    # Max jobs claimed per tick by ONE worker.
    sync_job_batch_size: int = 5
    # Cap on `status='running'` rows ACROSS all workers — counted before
    # claiming so multiple instances can't collectively hammer the school
    # APIs from our single egress IP (security review suggestion).
    sync_job_global_concurrency: int = 5
    sync_job_stale_lock_minutes: int = 10
    # Retriable-failure backoff: base * 2^(attempts-1), capped.
    sync_job_backoff_base_seconds: int = 300
    sync_job_backoff_cap_seconds: int = 3600
    # Pull-to-refresh per-user-per-job-type cooldown.
    sync_job_manual_cooldown_seconds: int = 60
    # Moodle webservice fetch timeout (sync worker, not login verify).
    moodle_fetch_timeout_seconds: float = 20.0
```

- [x] **Step 2: Write the failing tests (claim/recovery half)**

Create `server/tests/test_syncjobs_executor.py`:

```python
"""Executor: stale-lock recovery, claiming, global cap, execution paths."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    PushJob,
    User,
)
from server.db import build_session_factory
from server.syncjobs.executor import SyncWorker, run_sync_tick
from server.syncjobs.models import SyncJob, SyncPolicy, SyncRun
from server.syncjobs.moodle_client import (
    FetchedAssignment,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    ObtainedToken,
    SsoAuthFailed,
    SsoUnavailable,
)
from server.syncjobs.policies import ensure_default_policies
from server.sync.models import UserAssignment, UserChangeLog

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


class StubObtainer:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def obtain_token(self, *, username, password):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _worker(
    prepared_engine, test_settings, fetcher=None, obtainer=None
) -> SyncWorker:
    return SyncWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        cipher=_cipher(),
        fetcher=fetcher if fetcher is not None else StubFetcher(),
        token_obtainer=obtainer if obtainer is not None else StubObtainer(),
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


def _fa(aid: int) -> FetchedAssignment:
    return FetchedAssignment(
        moodle_course_id=7001,
        moodle_assignment_id=aid,
        course_name="資料結構",
        title=f"HW{aid}",
        due_at=datetime.now(UTC) + timedelta(days=7),
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
```

- [x] **Step 3: Run tests to verify they fail**

Run: `uv run pytest server/tests/test_syncjobs_executor.py -q`
Expected: FAIL with `ImportError` on `server.syncjobs.executor`

- [x] **Step 4: Implement `server/syncjobs/executor.py`** (full module — execution paths are exercised in Task 8 but implemented now so the file lands whole)

```python
"""Server-side sync job executor (sync-and-push spec §4).

Claim/execute split: one short transaction recovers stale locks, another
claims up to `sync_job_batch_size` due jobs (`FOR UPDATE SKIP LOCKED`,
joined to an enabled+in-window policy, global running-count cap), then
each job executes in its own transaction so one failure can't poison the
batch. Jobs run sequentially within a tick — deliberate politeness toward
the school APIs behind our single egress IP.

Failure taxonomy → outcome:
* `CredentialInvalid`            → account invalidated, ALL user jobs
                                   disabled, reauth push queued. No retry.
* `MoodleRateLimited`            → retriable, backoff, last_error
                                   'school_rate_limited'.
* `MoodleUnreachable`/`SsoUnavailable`/unexpected
                                 → retriable, backoff, last_error
                                   'sync_failed:<detail>'.
Retriable failures exceeding max_attempts land in status 'failed' until
pull-to-refresh or re-login revives them.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.crypto import CredentialCipher
from server.config import Settings
from server.db import session_scope
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential_blob,
    mark_credentials_invalid,
    refresh_moodle_token,
)
from server.syncjobs.models import (
    SyncJob,
    SyncJobStatus,
    SyncPolicy,
    SyncRun,
    SyncRunStatus,
)
from server.syncjobs.moodle_client import (
    AssignmentFetcher,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoUnavailable,
    TokenObtainer,
)

logger = structlog.get_logger(__name__)

ERROR_CREDENTIAL_INVALID = "credential_invalid"
ERROR_SCHOOL_RATE_LIMITED = "school_rate_limited"
ERROR_SYNC_FAILED = "sync_failed"

_DEFAULT_INTERVAL_SECONDS = 28800
_DEFAULT_PRIORITY = 100


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class SyncWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: CredentialCipher
    fetcher: AssignmentFetcher
    token_obtainer: TokenObtainer
    worker_id: str


async def run_sync_tick(worker: SyncWorker) -> int:
    """One scheduler tick: recover stale locks, claim due jobs, execute
    them sequentially. Returns the number of jobs executed."""
    await _recover_stale_jobs(worker)
    claimed = await _claim_due_jobs(worker)
    for job_id, run_id in claimed:
        await _execute_job(worker, job_id=job_id, run_id=run_id)
    return len(claimed)


async def _recover_stale_jobs(worker: SyncWorker) -> None:
    cutoff = datetime.now(UTC) - timedelta(
        minutes=worker.settings.sync_job_stale_lock_minutes
    )
    async with session_scope(worker.session_factory) as session:
        jobs = (
            (
                await session.execute(
                    select(SyncJob)
                    .where(
                        SyncJob.status == SyncJobStatus.running.value,
                        SyncJob.locked_at < cutoff,
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
            job.status = SyncJobStatus.pending.value
            job.locked_by = None
            job.locked_at = None
        await session.execute(
            update(SyncRun)
            .where(
                SyncRun.sync_job_id.in_([j.id for j in jobs]),
                SyncRun.status == SyncRunStatus.running.value,
            )
            .values(
                status=SyncRunStatus.failed.value,
                error="stale_lock_recovered",
                finished_at=now,
            )
        )
        logger.warning(
            "syncjobs.stale_recovered", count=len(jobs), worker=worker.worker_id
        )


async def _claim_due_jobs(worker: SyncWorker) -> list[tuple[int, int]]:
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        running = (
            await session.execute(
                select(func.count())
                .select_from(SyncJob)
                .where(SyncJob.status == SyncJobStatus.running.value)
            )
        ).scalar_one()
        capacity = min(
            worker.settings.sync_job_batch_size,
            worker.settings.sync_job_global_concurrency - running,
        )
        if capacity <= 0:
            return []

        jobs = (
            (
                await session.execute(
                    select(SyncJob)
                    .join(SyncPolicy, SyncPolicy.job_type == SyncJob.job_type)
                    .where(
                        SyncJob.status == SyncJobStatus.pending.value,
                        SyncJob.run_after <= now,
                        SyncPolicy.enabled.is_(True),
                        sa.or_(
                            SyncPolicy.active_from.is_(None),
                            SyncPolicy.active_from <= now,
                        ),
                        sa.or_(
                            SyncPolicy.active_until.is_(None),
                            SyncPolicy.active_until > now,
                        ),
                    )
                    .order_by(SyncJob.priority, SyncJob.run_after)
                    .limit(capacity)
                    .with_for_update(skip_locked=True, of=SyncJob)
                )
            )
            .scalars()
            .all()
        )
        claimed: list[tuple[int, int]] = []
        for job in jobs:
            job.status = SyncJobStatus.running.value
            job.locked_by = worker.worker_id
            job.locked_at = now
            run = SyncRun(sync_job_id=job.id, user_id=job.user_id)
            session.add(run)
            await session.flush()
            claimed.append((job.id, run.id))
        return claimed


async def _execute_job(worker: SyncWorker, *, job_id: int, run_id: int) -> None:
    try:
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(SyncJob).where(SyncJob.id == job_id).with_for_update()
                )
            ).scalar_one()
            run = await session.get(SyncRun, run_id)
            policy = (
                await session.execute(
                    select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
                )
            ).scalar_one_or_none()

            account, blob = await load_credential_blob(
                session, worker.cipher, external_account_id=job.external_account_id
            )
            token = (blob.get("token_cache") or {}).get("moodle_token")

            fetched = None
            if isinstance(token, str) and token:
                try:
                    fetched = await worker.fetcher.fetch_assignments(token=token)
                except MoodleTokenInvalid:
                    logger.info(
                        "syncjobs.token_expired", account_id=account.id
                    )
            if fetched is None:
                token = await refresh_moodle_token(
                    session,
                    worker.cipher,
                    worker.token_obtainer,
                    account=account,
                    blob=blob,
                )
                fetched = await worker.fetcher.fetch_assignments(token=token)

            now = datetime.now(UTC)
            stats = await apply_fetched_assignments(
                session, user_id=job.user_id, fetched=fetched, now=now
            )

            if run is not None:
                run.status = SyncRunStatus.succeeded.value
                run.finished_at = now
                run.fetched_count = stats.fetched_count
                run.changed_count = stats.changed_count

            interval = (
                policy.default_interval_seconds
                if policy
                else _DEFAULT_INTERVAL_SECONDS
            )
            job.status = SyncJobStatus.pending.value
            job.run_after = now + timedelta(seconds=interval)
            job.attempts = 0
            job.priority = policy.priority if policy else _DEFAULT_PRIORITY
            job.locked_by = None
            job.locked_at = None
            job.last_success_at = now
            job.last_error = None
            logger.info(
                "syncjobs.run_succeeded",
                job_id=job_id,
                user_id=str(job.user_id),
                fetched=stats.fetched_count,
                changed=stats.changed_count,
            )
    except CredentialInvalid as exc:
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=ERROR_CREDENTIAL_INVALID,
            disable=True,
            detail=exc.reason,
        )
    except MoodleRateLimited:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id, error=ERROR_SCHOOL_RATE_LIMITED
        )
    except (MoodleUnreachable, SsoUnavailable) as exc:
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{str(exc)[:120]}",
        )
    except Exception as exc:  # unexpected — never kill the tick loop
        logger.exception("syncjobs.run_crashed", job_id=job_id)
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{type(exc).__name__}",
        )


async def _record_failure(
    worker: SyncWorker,
    *,
    job_id: int,
    run_id: int,
    error: str,
    disable: bool = False,
    detail: str | None = None,
) -> None:
    """Record a failure in a FRESH session — the work session may have
    rolled back (or be poisoned by a DB error)."""
    async with session_scope(worker.session_factory) as session:
        job = (
            await session.execute(
                select(SyncJob).where(SyncJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return
        now = datetime.now(UTC)
        run = await session.get(SyncRun, run_id)
        if run is not None and run.status == SyncRunStatus.running.value:
            run.status = SyncRunStatus.failed.value
            run.finished_at = now
            run.error = error if detail is None else f"{error}:{detail}"

        job.locked_by = None
        job.locked_at = None
        job.last_failure_at = now
        job.last_error = error

        if disable:
            account = await session.get(
                type(job).external_account_id.entity.class_, 0
            )  # placeholder, replaced below
        if disable:
            from server.auth.models import ExternalAccount

            account = await session.get(
                ExternalAccount, job.external_account_id
            )
            if account is not None:
                await mark_credentials_invalid(
                    session, account=account, user_id=job.user_id, error=error
                )
            else:
                job.status = SyncJobStatus.disabled.value
            logger.warning(
                "syncjobs.run_disabled", job_id=job_id, error=error
            )
            return

        job.attempts += 1
        if job.attempts >= job.max_attempts:
            job.status = SyncJobStatus.failed.value
            logger.warning(
                "syncjobs.run_failed_terminal",
                job_id=job_id,
                attempts=job.attempts,
                error=error,
            )
        else:
            backoff = min(
                worker.settings.sync_job_backoff_base_seconds
                * 2 ** (job.attempts - 1),
                worker.settings.sync_job_backoff_cap_seconds,
            )
            job.status = SyncJobStatus.pending.value
            job.run_after = now + timedelta(seconds=backoff)
            logger.info(
                "syncjobs.run_retry_scheduled",
                job_id=job_id,
                attempts=job.attempts,
                backoff_seconds=backoff,
                error=error,
            )
```

**Note:** delete the stray placeholder block (`account = await session.get(type(job)...)`) — keep only the clean `if disable:` block with the local `ExternalAccount` import (move the import to the top of the file with the other imports instead; it does not create a cycle).

- [x] **Step 5: Run the Task-7 tests**

Run: `uv run pytest server/tests/test_syncjobs_executor.py -q` → PASS (5 tests)

- [x] **Step 6: Lint + commit**

```bash
git add server/config.py server/syncjobs/executor.py server/tests/test_syncjobs_executor.py
git commit -m "feat(SyncJobs): add sync job executor with claim and recovery

- claim 交易：FOR UPDATE SKIP LOCKED + join enabled/active-window policy，
  每 tick 上限 5 筆並先數 running 數納入全域並發上限（防多 worker 打爆學校 API）
- stale lock（running 超過 10 分鐘）回收為 pending，孤兒 sync_runs 標記 failed
- 新增 sync_job_* 與 moodle_fetch_timeout_seconds 設定"
```

---

### Task 8: Executor execution-path tests

**Files:**
- Modify: `server/tests/test_syncjobs_executor.py` (append tests)

- [x] **Step 1: Append execution-path tests**

```python
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


async def test_expired_token_refreshed_once_then_fetch_retried(
    db_session, prepared_engine, test_settings
):
    user, account, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(results=[_fa(1)], errors=[MoodleTokenInvalid("dead")])
    obtainer = StubObtainer(
        result=ObtainedToken(token="fresh-tok", private_token=None)
    )
    worker = _worker(
        prepared_engine, test_settings, fetcher=fetcher, obtainer=obtainer
    )
    await run_sync_tick(worker)

    assert obtainer.calls == 1
    assert fetcher.calls == ["tok-1", "fresh-tok"]
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.last_success_at is not None


async def test_sso_auth_failure_disables_all_jobs_no_retry(
    db_session, prepared_engine, test_settings
):
    user, account, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(errors=[MoodleTokenInvalid("dead")])
    obtainer = StubObtainer(error=SsoAuthFailed("invalidlogin"))
    worker = _worker(
        prepared_engine, test_settings, fetcher=fetcher, obtainer=obtainer
    )
    await run_sync_tick(worker)

    assert obtainer.calls == 1  # the iron rule: exactly one SSO attempt
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

    # Next tick: disabled job is never claimed, SSO never re-attempted.
    await run_sync_tick(worker)
    assert obtainer.calls == 1


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


async def test_sso_network_failure_is_retriable_not_disabling(
    db_session, prepared_engine, test_settings
):
    _, account, job = await _setup_user_job(db_session)
    fetcher = StubFetcher(errors=[MoodleTokenInvalid("dead")])
    obtainer = StubObtainer(error=SsoUnavailable("timeout"))
    worker = _worker(
        prepared_engine, test_settings, fetcher=fetcher, obtainer=obtainer
    )
    await run_sync_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"  # backoff retry, NOT disabled
    assert job.attempts == 1
    await db_session.refresh(account)
    assert account.credential_status == "active"
```

- [x] **Step 2: Run tests**

Run: `uv run pytest server/tests/test_syncjobs_executor.py -q`
Expected: all PASS. Fix executor bugs surfaced here (this is the GREEN step for the execution half implemented in Task 7).

- [x] **Step 3: Full suite + lint + commit**

Run: `uv run pytest server/tests/ -q` → all green.

```bash
git add server/tests/test_syncjobs_executor.py server/syncjobs/executor.py
git commit -m "test(SyncJobs): cover executor execution paths

- 成功路徑：upsert + changelog + run_after=now+interval + attempts 歸零
- token 過期：單次 SSO 換 token 後重抓
- SSO 認證失敗：一次即停用全部 jobs + reauth push，後續 tick 不再嘗試
- 網路錯誤 backoff、超過 max_attempts 轉 failed、429 記 school_rate_limited
- SSO 網路錯誤走 retry 不停用（與認證錯誤分流）"
```

---

### Task 9: Scheduler + lifespan wiring

**Files:**
- Modify: `server/scheduler/runtime.py`
- Modify: `server/main.py`
- Modify: `server/tests/conftest.py` (neuter the new tick)

- [x] **Step 1: Neuter the tick in tests FIRST**

In `server/tests/conftest.py` `test_settings`, add alongside `scheduler_tick_seconds=99999`:

```python
        sync_job_tick_seconds=99999,
```

- [x] **Step 2: Register the job in `server/scheduler/runtime.py`**

Add imports:

```python
from server.syncjobs.executor import SyncWorker, run_sync_tick
```

Change `build_scheduler` signature:

```python
def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    sync_worker: SyncWorker | None = None,
) -> AsyncIOScheduler:
```

Inside, after the existing job definitions add:

```python
    if sync_worker is not None:

        async def sync_jobs_tick() -> None:
            await run_sync_tick(sync_worker)

        scheduler.add_job(
            sync_jobs_tick,
            trigger=IntervalTrigger(seconds=settings.sync_job_tick_seconds),
            id="sync_jobs_tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
```

(Update the docstring job list accordingly.)

- [x] **Step 3: Build the worker in `server/main.py` lifespan**

Add imports at top:

```python
from server.syncjobs.executor import SyncWorker, default_worker_id
from server.syncjobs.moodle_client import HttpAssignmentFetcher, HttpTokenObtainer
```

In lifespan, replace `scheduler = build_scheduler(session_factory, router, settings)` with:

```python
    cipher = app.state.credential_cipher
    sync_worker = None
    if cipher is not None:
        sync_worker = SyncWorker(
            session_factory=session_factory,
            settings=settings,
            cipher=cipher,
            fetcher=HttpAssignmentFetcher(
                base_url=settings.moodle_base_url,
                timeout_seconds=settings.moodle_fetch_timeout_seconds,
            ),
            token_obtainer=HttpTokenObtainer(
                base_url=settings.moodle_base_url,
                timeout_seconds=settings.moodle_fetch_timeout_seconds,
            ),
            worker_id=default_worker_id(),
        )
    else:
        logger.warning("syncjobs.disabled_no_credential_keys")
    scheduler = build_scheduler(
        session_factory, router, settings, sync_worker=sync_worker
    )
```

- [x] **Step 4: Verify**

Run: `uv run pytest server/tests/ -q` → full suite green (lifespan starts the scheduler in every e2e test; the 99999s interval keeps the tick inert).

- [x] **Step 5: Lint + commit**

```bash
git add server/scheduler/runtime.py server/main.py server/tests/conftest.py
git commit -m "feat(SyncJobs): wire sync job executor into APScheduler

- build_scheduler 接受 sync_worker，註冊 30 秒 sync_jobs_tick
- lifespan 以 credential_cipher 建 worker；無金鑰時記 log 並停用
- 測試環境 sync_job_tick_seconds=99999 中和背景 tick"
```

---

### Task 10: Login provisioning

**Files:**
- Create: `server/syncjobs/provisioning.py`
- Modify: `server/auth/service.py`
- Test: `server/tests/test_syncjobs_provisioning.py`

- [x] **Step 1: Write the failing test**

```python
"""Login creates/revives sync_jobs (e2e through /v3/auth/login)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "b11203058",
    "password": "pw",
    "moodle_token": "tok",
    "moodle_private_token": None,
    "device_info": {
        "client_device_id": "dev-1",
        "platform": "ios",
        "device_name": "iPhone",
        "app_version": "3.0.0",
        "os_version": "26.0",
    },
}


async def _login(client):
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11203058")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    return response.json()


async def test_login_creates_moodle_assignments_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        jobs = (await session.execute(select(SyncJob))).scalars().all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == "moodle_assignments"
    assert job.status == "pending"
    assert job.max_attempts == 3


async def test_relogin_revives_disabled_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
        job.status = "disabled"
        job.attempts = 3
        job.last_error = "credential_invalid"
        await session.commit()

    await _login(client)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_error is None


async def test_relogin_does_not_reset_healthy_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
        job.attempts = 2  # mid-backoff but not disabled
        await session.commit()

    await _login(client)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
    assert job.attempts == 2  # untouched
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_syncjobs_provisioning.py -q`
Expected: first test FAILS — `len(jobs) == 0` (login doesn't create jobs yet).

- [x] **Step 3: Implement `server/syncjobs/provisioning.py`**

```python
"""Create sync_jobs at login (migration plan Phase 3, handoff decision:
login-time upsert, not backfill).

Jobs are created for every job type the executor can actually run
(`HANDLED_JOB_TYPES`); the executor filters by policy enabled/active
window at claim time, so admin policy toggles affect all users without
backfill. A `disabled` job is revived on re-login — the user just stored
fresh credentials. Healthy jobs are left untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.syncjobs.models import SyncJob, SyncJobStatus, SyncJobType, SyncPolicy

# Phase 3 implements the Moodle assignment fetcher only. ntust_courses /
# calendar / grades policies exist but have no per-user jobs until their
# fetchers land.
HANDLED_JOB_TYPES: tuple[str, ...] = (SyncJobType.moodle_assignments.value,)


async def ensure_sync_jobs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    external_account_id: int,
) -> None:
    now = datetime.now(UTC)
    policies = {
        p.job_type: p
        for p in (await session.execute(select(SyncPolicy))).scalars()
    }
    values = [
        {
            "user_id": user_id,
            "external_account_id": external_account_id,
            "job_type": job_type,
            "run_after": now,
            "priority": policies[job_type].priority
            if job_type in policies
            else 100,
            "max_attempts": policies[job_type].max_attempts
            if job_type in policies
            else 3,
        }
        for job_type in HANDLED_JOB_TYPES
    ]
    await session.execute(
        pg_insert(SyncJob)
        .values(values)
        .on_conflict_do_update(
            index_elements=["user_id", "job_type"],
            set_={
                "status": SyncJobStatus.pending.value,
                "attempts": 0,
                "run_after": now,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "external_account_id": external_account_id,
            },
            where=(SyncJob.status == SyncJobStatus.disabled.value),
        )
    )
```

- [x] **Step 4: Hook into login**

In `server/auth/service.py` add import:

```python
from server.syncjobs.provisioning import ensure_sync_jobs
```

In `login()`, after `device = await _upsert_device(...)` (line ~128) add:

```python
        await ensure_sync_jobs(
            session, user_id=user.id, external_account_id=account.id
        )
```

- [x] **Step 5: Run tests**

Run: `uv run pytest server/tests/test_syncjobs_provisioning.py server/tests/test_auth_login.py -q` → PASS (login tests must stay green).

- [x] **Step 6: Lint + commit**

```bash
git add server/syncjobs/provisioning.py server/auth/service.py server/tests/test_syncjobs_provisioning.py
git commit -m "feat(SyncJobs): provision sync jobs at login

- login 成功後為 HANDLED_JOB_TYPES upsert sync_jobs（採 login-time 而非 backfill）
- 重新登入時 disabled job 復活（status/attempts/last_error 重置）
- 健康中的 job 不受重登入影響；policy 開關由 executor claim 時過濾"
```

---

### Task 11: Pull-to-refresh endpoint

**Files:**
- Create: `server/routes/sync_jobs.py` (user router half)
- Modify: `server/main.py` (`_mount_api_v3`)
- Test: `server/tests/test_sync_jobs_api.py`

- [x] **Step 1: Write the failing test**

```python
"""POST /v3/sync-jobs/run-now — pull-to-refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "b11203058",
    "password": "pw",
    "moodle_token": "tok",
    "moodle_private_token": None,
    "device_info": {
        "client_device_id": "dev-1",
        "platform": "ios",
        "device_name": "iPhone",
        "app_version": "3.0.0",
        "os_version": "26.0",
    },
}


async def _login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11203058")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _get_job(client) -> SyncJob:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (await session.execute(select(SyncJob))).scalar_one()


async def _set_job(client, **values):
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
        for key, value in values.items():
            setattr(job, key, value)
        await session.commit()


async def test_run_now_queues_high_priority(client):
    headers = await _login(client)
    # Make the job look like a normal scheduled one in the future.
    await _set_job(
        client,
        run_after=datetime.now(UTC) + timedelta(hours=8),
        cursor={},
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["status"] == "pending"
    assert body["priority"] == 1

    job = await _get_job(client)
    assert job.priority == 1
    assert job.run_after <= datetime.now(UTC)
    assert "manual_requested_at" in job.cursor


async def test_run_now_cooldown_429(client):
    headers = await _login(client)
    await _set_job(
        client, run_after=datetime.now(UTC) + timedelta(hours=8), cursor={}
    )
    first = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert first.status_code == 200

    await _set_job(client, run_after=datetime.now(UTC) + timedelta(hours=8))
    second = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert second.status_code == 429
    assert second.json()["detail"]["error"] == "cooldown"


async def test_run_now_running_job_returns_status_without_requeue(client):
    headers = await _login(client)
    await _set_job(
        client,
        status="running",
        locked_by="worker-1",
        locked_at=datetime.now(UTC),
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is False
    assert body["status"] == "running"


async def test_run_now_disabled_returns_credential_invalid(client):
    headers = await _login(client)
    await _set_job(client, status="disabled", last_error="credential_invalid")
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "credential_invalid"


async def test_run_now_revives_failed_job(client):
    headers = await _login(client)
    await _set_job(
        client,
        status="failed",
        attempts=3,
        last_error="sync_failed:net",
        cursor={},
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is True
    assert body["last_error_code"] == "sync_failed"

    job = await _get_job(client)
    assert job.status == "pending"
    assert job.attempts == 0


async def test_run_now_rejects_unknown_job_type(client):
    headers = await _login(client)
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=grades", headers=headers
    )
    assert response.status_code == 422  # not in HANDLED_JOB_TYPES literal


async def test_run_now_requires_auth(client):
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments"
    )
    assert response.status_code == 401
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_sync_jobs_api.py -q`
Expected: 404s — route doesn't exist.

- [x] **Step 3: Implement the user router in `server/routes/sync_jobs.py`**

```python
"""/v3/sync-jobs — pull-to-refresh; /v3/admin/sync-policies — dashboard.

Pull-to-refresh never touches school APIs inline: it re-prioritizes the
user's existing sync job (priority=1, run_after=now) and the 30s executor
tick picks it up. The per-user cooldown lives in `sync_jobs.cursor`
(JSONB) so it survives restarts and works across instances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import ExternalAccount
from server.auth.service import PROVIDER_NTUST_SSO
from server.db import SessionDep
from server.security import require_shared_secret
from server.syncjobs.models import SyncJob, SyncJobStatus
from server.syncjobs.provisioning import ensure_sync_jobs

router = APIRouter(prefix="/sync-jobs", tags=["sync-jobs"])
logger = structlog.get_logger(__name__)

HandledJobType = Literal["moodle_assignments"]

ERROR_CODES = ("credential_invalid", "school_rate_limited", "sync_failed")


def _last_error_code(job: SyncJob) -> str | None:
    if job.status == SyncJobStatus.disabled.value:
        return "credential_invalid"
    if job.last_error is None:
        return None
    for code in ERROR_CODES:
        if job.last_error.startswith(code):
            return code
    return "sync_failed"


def _status_payload(job: SyncJob, *, queued: bool) -> dict:
    return {
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "run_after": job.run_after.isoformat(),
        "attempts": job.attempts,
        "last_success_at": (
            job.last_success_at.isoformat() if job.last_success_at else None
        ),
        "last_failure_at": (
            job.last_failure_at.isoformat() if job.last_failure_at else None
        ),
        "last_error_code": _last_error_code(job),
        "queued": queued,
    }


@router.post("/run-now")
async def run_now(
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    job_type: HandledJobType = Query(),
):
    settings = request.app.state.settings
    now = datetime.now(UTC)

    job = (
        await session.execute(
            select(SyncJob)
            .where(SyncJob.user_id == auth.user_id, SyncJob.job_type == job_type)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if job is None:
        account = (
            await session.execute(
                select(ExternalAccount).where(
                    ExternalAccount.user_id == auth.user_id,
                    ExternalAccount.provider == PROVIDER_NTUST_SSO,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "sync_not_provisioned"},
            )
        await ensure_sync_jobs(
            session, user_id=auth.user_id, external_account_id=account.id
        )
        job = (
            await session.execute(
                select(SyncJob)
                .where(
                    SyncJob.user_id == auth.user_id,
                    SyncJob.job_type == job_type,
                )
                .with_for_update()
            )
        ).scalar_one()

    if job.status == SyncJobStatus.disabled.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "credential_invalid"},
        )

    if job.status == SyncJobStatus.running.value:
        return _status_payload(job, queued=False)
    if (
        job.status == SyncJobStatus.pending.value
        and job.priority <= 1
        and job.run_after <= now
    ):
        # Already queued at high priority — don't burn the cooldown.
        return _status_payload(job, queued=False)

    cooldown = timedelta(seconds=settings.sync_job_manual_cooldown_seconds)
    requested_raw = (job.cursor or {}).get("manual_requested_at")
    if isinstance(requested_raw, str):
        try:
            requested_at = datetime.fromisoformat(requested_raw)
        except ValueError:
            requested_at = None
        if requested_at is not None and now - requested_at < cooldown:
            retry_after = int(
                (requested_at + cooldown - now).total_seconds()
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "cooldown",
                    "retry_after_seconds": max(retry_after, 1),
                },
            )

    if job.status == SyncJobStatus.failed.value:
        job.attempts = 0  # manual revive
    job.status = SyncJobStatus.pending.value
    job.priority = 1
    job.run_after = now
    # Reassign (not mutate) so SQLAlchemy detects the JSONB change.
    job.cursor = {**(job.cursor or {}), "manual_requested_at": now.isoformat()}
    logger.info(
        "syncjobs.run_now",
        user_id=str(auth.user_id),
        job_type=job_type,
    )
    return _status_payload(job, queued=True)
```

- [x] **Step 4: Mount in `server/main.py`**

Add `from server.routes import sync_jobs as sync_jobs_routes` to the imports, then in `_mount_api_v3` add:

```python
    app.include_router(sync_jobs_routes.router, prefix=prefix)
```

- [x] **Step 5: Run tests**

Run: `uv run pytest server/tests/test_sync_jobs_api.py -q` → PASS

- [x] **Step 6: Lint + commit**

```bash
git add server/routes/sync_jobs.py server/main.py server/tests/test_sync_jobs_api.py
git commit -m "feat(SyncJobs): add pull-to-refresh run-now endpoint

- POST /v3/sync-jobs/run-now：priority=1、run_after=now，由 executor tick 執行
- 60 秒 per-user cooldown 存於 sync_jobs.cursor JSONB（多 instance 安全）
- running / 已高優先 pending 直接回現狀不重排；failed 手動觸發復活
- disabled 回 409 credential_invalid；last_error 映射三種錯誤碼"
```

---

### Task 12: Admin sync-policies endpoint

**Files:**
- Modify: `server/routes/sync_jobs.py` (admin router)
- Modify: `server/main.py` (mount)
- Test: `server/tests/test_admin_sync_policies.py`

- [x] **Step 1: Write the failing test**

```python
"""GET/PATCH /v3/admin/sync-policies — shared-secret protected."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_list_policies_returns_seeded_defaults(client):
    response = await client.get("/v3/admin/sync-policies")
    assert response.status_code == 200
    body = {p["job_type"]: p for p in response.json()["policies"]}
    assert body["moodle_assignments"]["enabled"] is True
    assert body["moodle_assignments"]["default_interval_seconds"] == 28800
    assert body["ntust_courses"]["enabled"] is False
    assert body["calendar"]["default_interval_seconds"] == 604800


async def test_patch_updates_interval_and_enabled(client):
    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"enabled": True, "default_interval_seconds": 3600},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["default_interval_seconds"] == 3600


async def test_patch_sets_and_clears_active_window(client):
    window_start = datetime(2026, 6, 1, tzinfo=UTC).isoformat()
    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"active_from": window_start},
    )
    assert response.status_code == 200
    assert response.json()["active_from"] is not None

    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"active_from": None},
    )
    assert response.status_code == 200
    assert response.json()["active_from"] is None


async def test_patch_unknown_job_type_404(client):
    response = await client.patch(
        "/v3/admin/sync-policies/bogus", json={"enabled": True}
    )
    assert response.status_code == 404


async def test_patch_rejects_nonpositive_interval(client):
    response = await client.patch(
        "/v3/admin/sync-policies/calendar",
        json={"default_interval_seconds": 0},
    )
    assert response.status_code == 422


async def test_admin_requires_shared_secret_when_configured(client):
    client.app.state.settings.api_shared_secret = "sekrit"
    try:
        denied = await client.patch(
            "/v3/admin/sync-policies/calendar", json={"enabled": False}
        )
        assert denied.status_code == 401
        allowed = await client.patch(
            "/v3/admin/sync-policies/calendar",
            json={"enabled": False},
            headers={"X-Push-Token": "sekrit"},
        )
        assert allowed.status_code == 200
    finally:
        client.app.state.settings.api_shared_secret = ""
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_admin_sync_policies.py -q`
Expected: 404s — admin routes don't exist.

- [x] **Step 3: Append admin router to `server/routes/sync_jobs.py`**

```python
from pydantic import BaseModel, ConfigDict, Field

from server.syncjobs.models import SyncJobType, SyncPolicy

admin_router = APIRouter(
    prefix="/admin/sync-policies",
    tags=["admin"],
    dependencies=[Depends(require_shared_secret)],
)


class SyncPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    default_interval_seconds: int | None = Field(default=None, gt=0)
    active_from: datetime | None = None
    active_until: datetime | None = None
    priority: int | None = None
    max_attempts: int | None = Field(default=None, gt=0)


def _policy_to_dict(policy: SyncPolicy) -> dict:
    return {
        "job_type": policy.job_type,
        "enabled": policy.enabled,
        "default_interval_seconds": policy.default_interval_seconds,
        "active_from": (
            policy.active_from.isoformat() if policy.active_from else None
        ),
        "active_until": (
            policy.active_until.isoformat() if policy.active_until else None
        ),
        "priority": policy.priority,
        "max_attempts": policy.max_attempts,
        "updated_at": policy.updated_at.isoformat(),
    }


@admin_router.get("")
async def list_policies(session: SessionDep):
    policies = (
        (await session.execute(select(SyncPolicy).order_by(SyncPolicy.job_type)))
        .scalars()
        .all()
    )
    return {"policies": [_policy_to_dict(p) for p in policies]}


@admin_router.patch("/{job_type}")
async def patch_policy(
    job_type: str, payload: SyncPolicyPatch, session: SessionDep
):
    policy = (
        await session.execute(
            select(SyncPolicy).where(SyncPolicy.job_type == job_type)
        )
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_job_type"},
        )
    # exclude_unset: explicit nulls clear the active window, absent
    # fields are left untouched.
    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(policy, field_name, value)
    logger.info("syncjobs.policy_patched", job_type=job_type, **updates)
    return _policy_to_dict(policy)
```

(`datetime` is already imported at the top of the file.)

- [x] **Step 4: Mount in `server/main.py` `_mount_api_v3`**

```python
    app.include_router(sync_jobs_routes.admin_router, prefix=prefix)
```

- [x] **Step 5: Run tests**

Run: `uv run pytest server/tests/test_admin_sync_policies.py -q` → PASS

- [x] **Step 6: Lint + commit**

```bash
git add server/routes/sync_jobs.py server/main.py server/tests/test_admin_sync_policies.py
git commit -m "feat(SyncJobs): add admin sync-policies endpoints

- GET /v3/admin/sync-policies 列出全部 policy（dashboard 用）
- PATCH /v3/admin/sync-policies/{job_type} 調整 interval/enabled/active window
- 沿用 X-Push-Token shared secret（require_shared_secret）
- exclude_unset 語意：明確傳 null 可清除 active window"
```

---

### Task 13: Final verification + code review

- [x] **Step 1: Full test suite**

Run: `uv run pytest server/tests/ -q`
Expected: all tests pass (~330+). **Read the output text — do not trust exit codes.**

- [x] **Step 2: Lint all new files**

Run: `uv run ruff check server/syncjobs/ server/routes/sync_jobs.py server/tests/test_syncjobs_*.py server/tests/test_sync_jobs_api.py server/tests/test_admin_sync_policies.py server/tests/test_moodle_fetch_client.py`
Expected: clean. Also `uv run ruff check server/config.py server/main.py server/scheduler/runtime.py server/auth/service.py` (only complain about pre-existing issues if any — don't touch unrelated history).

- [x] **Step 3: Migration roundtrip re-verification** (same commands as Task 3 Step 4, fresh scratch DB).

- [x] **Step 4: Code review**

Dispatch the code-reviewer agent over `git diff feat/user-sync-phase2...HEAD -- server/`. Fix HIGH and MEDIUM findings; commit fixes as `fix(SyncJobs): ...`.

- [x] **Step 5: Summarize + ask the user**

Summarize changes; ask: push & open PR / merge locally / continue to Phase 4.

---

## Self-review notes

- Spec coverage: tables+DDL (T1/T3), seed values (T2/T3), executor with SKIP LOCKED + batch 5 + stale recovery + global cap (T7/T8), iron rule (T5/T8), changelog writes (T6), login provisioning (T10), pull-to-refresh w/ DB cooldown + error codes (T11), admin PATCH (T12), reauth push_jobs row (T5), 410/full-sync untouched (Phase 2).
- Out of scope (recorded): submission status fetch, ntust_courses/calendar/grades fetchers, co-fetch optimization, account deletion (review 1.6), bulletin dual-track (review 1.8).
