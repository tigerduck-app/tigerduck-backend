"""Alembic environment — reads DB URL from server Settings, target_metadata from Base."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import Base and every model module so `Base.metadata` is complete.
#
# This list is load-bearing, not cosmetic. Alembic diffs the database against
# `Base.metadata`, so a model module that is never imported here is invisible
# to autogenerate -- and autogenerate writes a `drop_table` for every table it
# cannot see. `server.models` pulls in auth/sync/syncjobs transitively; the two
# below have no such importer, and their four tables (academic_holidays,
# semester_terms, system_settings, user_holiday_overrides) were being proposed
# for deletion on every autogenerate run until this import was added.
#
# Adding a model module anywhere in `server/`? Make sure something in this
# chain imports it, then confirm with `alembic check`.
from server.config import get_settings
from server.db import Base
from server import models  # noqa: F401  (register tables on Base.metadata)
from server.bulletins import models as _bulletin_models  # noqa: F401
from server.academic_calendar import models as _academic_calendar_models  # noqa: F401
from server import system_settings as _system_settings_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
