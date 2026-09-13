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


class SyncLogEntry(Base):
    """Per-user sync event log. Written by `server/syncjobs/log_entries.py`
    and read by the portal's Moodle logs page, both with raw SQL -- writes are
    best-effort and must never fail a sync, so they bypass the ORM.

    The table is created by `4e6c604ad58c` with `CREATE TABLE IF NOT EXISTS`
    rather than `op.create_table`, which means Alembic's autogenerate never
    learned about it from the migration either. Mapping it here is what stops
    autogenerate proposing `op.drop_table("sync_log_entries")` -- it diffs the
    database against `Base.metadata`, and a table absent from the metadata
    reads as one the models want gone.

    Deliberately un-modelled details, kept as-is so the mapping matches the
    live schema: `user_id` carries no foreign key (a log outlives the rows it
    describes), and the retention sweep in `portal/app/routes/deregister.py`
    deletes by `user_id` directly."""

    __tablename__ = "sync_log_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    level: Mapped[str] = mapped_column(String(8), default="INFO", server_default="INFO")
    source: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_sync_log_entries_user", "user_id", sa.text("ts DESC")),
    )
