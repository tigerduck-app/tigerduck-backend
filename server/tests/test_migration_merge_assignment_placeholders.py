"""The migration that merges the apps' assignment placeholders (course id 0)
into the server's rows, run by Alembic on a database of its own the way a
deploy runs it, over rows for each case it decides."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.db import build_session_factory
from server.push.reminders import scan_assignment_reminders
from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

BEFORE = "df3948adb76a"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
DAY = timedelta(days=1)


async def _user(conn: AsyncConnection) -> uuid.UUID:
    return (
        await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
    ).scalar_one()


async def _row(
    conn: AsyncConnection,
    user: uuid.UUID,
    course: int,
    aid: int,
    *,
    deleted: datetime | None = None,
    submitted_at: datetime | None = None,
    submitted: bool | None = None,
    due: datetime | None = None,
) -> int:
    return (
        await conn.execute(
            text(
                "INSERT INTO user_assignments (user_id, moodle_course_id,"
                " moodle_assignment_id, title, deleted_at, provider_is_submitted,"
                " provider_submitted_at, due_at)"
                " VALUES (:user, :course, :aid, 'HW', :deleted, :submitted,"
                " :submitted_at, :due) RETURNING id"
            ),
            {
                "user": user,
                "course": course,
                "aid": aid,
                "deleted": deleted,
                "submitted": bool(submitted or submitted_at),
                "submitted_at": submitted_at,
                "due": due,
            },
        )
    ).scalar_one()


async def _mark(
    conn: AsyncConnection,
    user: uuid.UUID,
    row: int,
    status: str,
    at: datetime,
    *,
    note: str | None = None,
    note_at: datetime | None = None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO user_assignment_overrides (user_id, user_assignment_id,"
            " local_status, local_status_updated_at, local_status_received_at,"
            " note, note_updated_at)"
            " VALUES (:user, :row, :status, :at, :at, :note, :note_at)"
        ),
        {
            "user": user,
            "row": row,
            "status": status,
            "at": at,
            "note": note,
            "note_at": note_at,
        },
    )


async def _seed(conn: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """One user's rows, a case per assignment id, and a second user's
    placeholder under an id the first user has a server row of."""
    user = await _user(conn)
    # 1: a submission the app reported folds into the server's row.
    await _row(conn, user, 0, 1, deleted=T0 + DAY, submitted_at=T0 + DAY)
    await _row(conn, user, 7001, 1)
    # 2: a mark the apps show, on a live placeholder, moves.
    await _mark(conn, user, await _row(conn, user, 0, 2), "ignored", T0 + DAY)
    await _row(conn, user, 7001, 2)
    # 3 and 4: both rows marked; the later mark wins, from either row.
    await _mark(conn, user, await _row(conn, user, 0, 3), "locally_completed", T0 + 3 * DAY)
    await _mark(conn, user, await _row(conn, user, 7001, 3), "ignored", T0 + 2 * DAY)
    await _mark(conn, user, await _row(conn, user, 0, 4, deleted=T0 + DAY), "ignored", T0)
    await _mark(conn, user, await _row(conn, user, 7001, 4), "locally_completed", T0 + 2 * DAY)
    # 5: the fetch left the server's row out (a TA's course); it takes the mark.
    await _mark(conn, user, await _row(conn, user, 0, 5), "ignored", T0 + 2 * DAY)
    await _row(conn, user, 7001, 5, deleted=T0 + DAY)
    # 6: a mark /sync/full has not keyed since the server's row came stays
    # where it is, and the submission still folds in.
    placeholder = await _row(conn, user, 0, 6, deleted=T0 + DAY, submitted=True)
    await _mark(conn, user, placeholder, "ignored", T0)
    await _row(conn, user, 7001, 6)
    # 7: both rows gone, the placeholder after the server's: the mark is
    # keyed, so it moves.
    await _mark(conn, user, await _row(conn, user, 0, 7, deleted=T0 + 3 * DAY), "ignored", T0 + DAY)
    await _row(conn, user, 7001, 7, deleted=T0 + 2 * DAY)
    # 8: both rows gone, the server's after the placeholder and its mark:
    # hidden, so it stays.
    await _mark(conn, user, await _row(conn, user, 0, 8, deleted=T0 + DAY), "ignored", T0)
    await _row(conn, user, 7001, 8, deleted=T0 + 2 * DAY)
    # 9: no server row yet; the next fetch takes the placeholder over.
    await _mark(conn, user, await _row(conn, user, 0, 9), "ignored", T0)
    # 10: the later mark and the later note each win.
    await _mark(
        conn, user, await _row(conn, user, 0, 10), "ignored", T0 + DAY,
        note="Bring the lab sheet", note_at=T0 + 3 * DAY,
    )
    await _mark(conn, user, await _row(conn, user, 7001, 10), "locally_completed", T0 + 2 * DAY)
    # 11: the server's own submission time stays.
    await _row(conn, user, 0, 11, submitted_at=T0 + DAY)
    await _row(conn, user, 7001, 11, submitted_at=T0 + 2 * DAY)
    # 12: a cleared mark that keeps a note is no mark to hide, so it moves.
    await _mark(
        conn, user, await _row(conn, user, 0, 12, deleted=T0 + DAY), "none", T0,
        note="Bring the lab sheet", note_at=T0,
    )
    await _row(conn, user, 7001, 12)
    other = await _user(conn)
    await _row(conn, other, 0, 1)
    return user, other


async def _state(conn: AsyncConnection, user: uuid.UUID) -> dict[int, list[tuple]]:
    """Per assignment id, the user's rows as (course id, soft-deleted,
    submitted, mark), the placeholder first."""
    rows = await conn.execute(
        text(
            "SELECT a.moodle_assignment_id, a.moodle_course_id,"
            " a.deleted_at IS NOT NULL, a.provider_is_submitted, o.local_status"
            " FROM user_assignments a"
            " LEFT JOIN user_assignment_overrides o ON o.user_assignment_id = a.id"
            " WHERE a.user_id = :user"
            " ORDER BY a.moodle_assignment_id, a.moodle_course_id"
        ),
        {"user": user},
    )
    state: dict[int, list[tuple]] = {}
    for aid, *row in rows:
        state.setdefault(aid, []).append(tuple(row))
    return state


async def _scalar(conn: AsyncConnection, sql: str, user: uuid.UUID, aid: int):
    return (
        await conn.execute(
            text(sql + " WHERE a.user_id = :user AND a.moodle_assignment_id = :aid"),
            {"user": user, "aid": aid},
        )
    ).scalar_one()


async def test_upgrade_merges_each_placeholder_into_the_servers_row(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            user, other = await _seed(conn)

        _alembic(migration_db_url, "upgrade", "head")
        async with engine.connect() as conn:
            merged = await _state(conn, user)
            assert merged == {
                1: [(7001, False, True, None)],
                2: [(7001, False, False, "ignored")],
                3: [(7001, False, False, "locally_completed")],
                4: [(7001, False, False, "locally_completed")],
                5: [(7001, True, False, "ignored")],
                6: [(0, True, True, "ignored"), (7001, False, True, None)],
                7: [(7001, True, False, "ignored")],
                8: [(0, True, False, "ignored"), (7001, True, False, None)],
                9: [(0, False, False, "ignored")],
                10: [(7001, False, False, "locally_completed")],
                11: [(7001, False, True, None)],
                12: [(7001, False, False, "none")],
            }
            submitted_at = (
                "SELECT a.provider_submitted_at FROM user_assignments a"
            )
            assert await _scalar(conn, submitted_at, user, 1) == T0 + DAY
            assert await _scalar(conn, submitted_at, user, 11) == T0 + 2 * DAY
            mark_time = (
                "SELECT o.local_status_updated_at FROM user_assignments a"
                " JOIN user_assignment_overrides o ON o.user_assignment_id = a.id"
            )
            assert await _scalar(conn, mark_time, user, 3) == T0 + 3 * DAY
            note = (
                "SELECT o.note FROM user_assignments a"
                " JOIN user_assignment_overrides o ON o.user_assignment_id = a.id"
            )
            assert await _scalar(conn, note, user, 10) == "Bring the lab sheet"
            assert await _scalar(conn, note, user, 12) == "Bring the lab sheet"
            assert await _state(conn, other) == {1: [(0, False, False, None)]}

        # A rollback stamps the previous revision; the next start runs it again.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", "head")
        async with engine.connect() as conn:
            assert await _state(conn, user) == merged

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert await _state(conn, user) == merged
    finally:
        await engine.dispose()


async def _reminded(factory, test_settings) -> set[int]:
    await scan_assignment_reminders(factory, test_settings)
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT DISTINCT (payload->>'moodle_assignment_id')::int"
                " FROM push_jobs WHERE channel = 'assignment' AND status = 'pending'"
            )
        )
        return set(rows.scalars())


async def test_the_merged_rows_keep_the_reminder_scan_quiet(
    migration_db_url: str, test_settings
) -> None:
    """The scan reads only the server's live row of each assignment. Before
    the merge it reminds of HW1, handed in as the app reported, and HW2,
    ignored on the placeholder; after it, only of HW3."""
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        due = datetime.now(UTC) + timedelta(hours=30)
        async with engine.begin() as conn:
            user = await _user(conn)
            device = (
                await conn.execute(
                    text(
                        "INSERT INTO user_devices (user_id, client_device_id,"
                        " platform, app_version)"
                        " VALUES (:user, 'iphone', 'ios', '2.6.1') RETURNING id"
                    ),
                    {"user": user},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO device_push_tokens (device_id, provider,"
                    " token_kind, token_hash, token_value)"
                    " VALUES (:device, 'apns', 'standard', 'hash', 'token')"
                ),
                {"device": device},
            )
            await _row(conn, user, 0, 1, deleted=T0, submitted=True, due=due)
            await _row(conn, user, 7001, 1, due=due)
            await _mark(conn, user, await _row(conn, user, 0, 2, due=due), "ignored", T0)
            await _row(conn, user, 7001, 2, due=due)
            await _row(conn, user, 7001, 3, due=due)
        factory = build_session_factory(engine)
        assert await _reminded(factory, test_settings) == {1, 2, 3}

        _alembic(migration_db_url, "upgrade", "head")
        assert await _reminded(factory, test_settings) == {3}
    finally:
        await engine.dispose()
