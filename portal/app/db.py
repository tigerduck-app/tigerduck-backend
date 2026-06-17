"""Shared asyncpg pool for the operator API routes.

The portal joins the `tigerduck-db` network and reads/writes postgres
directly — it never calls the public backend API (40000). This keeps
admin traffic on the Cloudflare-guarded portal (40010) and off the
unauthenticated client surface.

The status page (`status.py`) connects ad-hoc per request; the operator
routes are hotter, so they share one pool created at startup. Both speak
the same database, so they agree on schema — the v3 tables (`users`,
`user_devices`, `device_push_tokens`, `device_lists`, `bulletins`,
`push_jobs`, …) owned by the backend container's Alembic migrations.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import Request

if TYPE_CHECKING:
    from fastapi import FastAPI


def _asyncpg_dsn(database_url: str) -> str:
    """asyncpg wants the bare `postgresql://` scheme, not the SQLAlchemy
    `postgresql+asyncpg://` the rest of the stack configures."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def open_pool(app: "FastAPI") -> None:
    """Create the shared pool, or leave it None when no DB is configured.

    A missing `database_url` is the intentional no-DB path (e.g. a portal
    booted only for the static SPA); operator routes raise a clear 503
    rather than crashing the whole app at startup.
    """
    database_url = app.state.settings.database_url
    if not database_url:
        app.state.pool = None
        return
    app.state.pool = await asyncpg.create_pool(
        _asyncpg_dsn(database_url),
        min_size=1,
        max_size=8,
        # Statements are simple and the schema is stable; a modest timeout
        # keeps a wedged query from pinning a connection forever.
        command_timeout=15.0,
    )


async def close_pool(app: "FastAPI") -> None:
    pool = getattr(app.state, "pool", None)
    if pool is not None:
        await pool.close()


def get_pool(request: Request) -> asyncpg.Pool:
    """FastAPI dependency: the live pool, or 503 if the DB isn't wired."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="portal database not configured (TIGERDUCK_DATABASE_URL unset)",
        )
    return pool
