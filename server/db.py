"""Async SQLAlchemy engine + session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import ColumnElement, Row, any_, delete, literal, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, QueryableAttribute

from server.config import Settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


#: Rows per INSERT for a statement that files a row per user or device.
#: asyncpg refuses a statement with more than 32,767 bind parameters, and a
#: row binds one per column it sets, so 2,000 rows leave room for 16.
INSERT_CHUNK_ROWS = 2000


def in_array(
    column: QueryableAttribute[Any], values: Iterable[Any]
) -> ColumnElement[bool]:
    """`column = ANY(:values)`, with `values` bound as one array parameter.

    `column.in_(values)` binds a parameter per value, and asyncpg refuses a
    statement with more than 32,767 of them; a lookup over every user's rows
    can pass that.
    """
    return column == any_(literal(list(values), ARRAY(column.type)))


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "server_settings": {
                "statement_timeout": _milliseconds(
                    settings.database_statement_timeout_seconds
                ),
                "idle_in_transaction_session_timeout": _milliseconds(
                    settings.database_idle_in_transaction_timeout_seconds
                ),
            }
        },
    )


def _milliseconds(seconds: float) -> str:
    return str(int(seconds * 1000))


async def lift_timeouts(session: AsyncSession) -> None:
    """Turn the server-side timeouts off for the rest of the session's
    current transaction.

    For a transaction that must not be cut off: one that sends pushes before
    it commits what it sent, where a cancelled commit would send them all
    again, and a retention purge, whose statements can outlast the limit.
    """
    await session.execute(
        text(
            "SELECT set_config('statement_timeout', '0', true), "
            "set_config('idle_in_transaction_session_timeout', '0', true)"
        )
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


#: Rows a retention purge deletes per transaction, so none holds its locks long.
PRUNE_BATCH_ROWS = 5000


async def delete_in_batches(
    factory: async_sessionmaker[AsyncSession],
    model: Any,
    *where: ColumnElement[bool],
    returning: Sequence[Any] = (),
    then: Callable[[AsyncSession, Sequence[Row[Any]]], Awaitable[None]] | None = None,
) -> int:
    """Delete `model`'s rows that match `where`, oldest id first and
    `PRUNE_BATCH_ROWS` per transaction, and return how many went.

    Each batch reads on from the last id the one before deleted. `then` runs
    in each batch's transaction with the rows it deleted: their `id` and the
    `returning` columns.
    """
    deleted, after = 0, 0
    while True:
        async with session_scope(factory) as session:
            # The last batch reads every row the purge keeps, past the limit.
            await lift_timeouts(session)
            oldest = (
                select(model.id)
                .where(model.id > after, *where)
                .order_by(model.id)
                .limit(PRUNE_BATCH_ROWS)
            )
            rows = (
                await session.execute(
                    delete(model)
                    .where(model.id.in_(oldest))
                    .returning(model.id, *returning)
                )
            ).all()
            if then is not None and rows:
                await then(session, rows)
        deleted += len(rows)
        if len(rows) < PRUNE_BATCH_ROWS:
            return deleted
        after = max(row.id for row in rows)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Usage: `async with session_scope(factory) as session:`.
    Commits on success, rolls back on exception."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session that auto-commits or rolls back."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_scope(factory) as session:
        yield session


# scope="function" commits before the response goes out, so a write that fails
# to commit answers 500 rather than a success the client then relies on.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
