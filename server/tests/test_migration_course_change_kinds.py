"""The migration that adds the assessment and online kinds and converts term
1151's notes, run by Alembic on a database of its own the way a deploy runs
it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "fefd80be557b"
BEFORE = "22370b5daa3b"

_SHAPE = text(
    "SELECT conname || ' ' || pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conrelid = 'course_changes'::regclass"
    " UNION ALL SELECT pg_get_functiondef(oid) FROM pg_proc"
    " WHERE proname = 'course_change_violations'"
)

_INSERT = text(
    "INSERT INTO course_changes (id, term_code, course_no, kind, mode, start_date, end_date,"
    " note_zh, note_en, created_by, updated_by) VALUES (:id, :term, 'CS2001301', :kind,"
    " 'day', '2026-11-05', '2026-11-05', :note_zh, :note_en, 'ops@example.com',"
    " 'ops@example.com')"
)
_ROWS = text("SELECT id, kind, note_zh, note_en FROM course_changes ORDER BY id")

_MIDTERMS = (14, 17, 18, 19, 20, 21, 33)
_ONLINE_TEXT = "因排程衝突，11/16 無實體課改為線上，請自行學習 Module 5。"
# The term's notes before the upgrade.
_NOTES = [
    *((n, "note", "期中考", "Midterm exam") for n in _MIDTERMS),
    (15, "note", "補課", "Make-up class"),
    (16, "note", "期中分組簡報", None),
    (23, "note", "期末考", "Final exam"),
    (35, "note", _ONLINE_TEXT, None),
    (36, "note", _ONLINE_TEXT, None),
    (54, "note", "期末報告", "Final Presentation"),
]
_CONVERTED = [
    *((n, "assessment", "期中考", "Midterm exam") for n in _MIDTERMS),
    (15, "note", "補課", "Make-up class"),
    (16, "assessment", "期中分組簡報", "Midterm group presentation"),
    (23, "assessment", "期末考", "Final exam"),
    (35, "online", _ONLINE_TEXT, None),
    (36, "online", _ONLINE_TEXT, None),
    (54, "assessment", "期末簡報", "Final presentation"),
]


async def _seed(engine: AsyncEngine, rows, term: str = "1151") -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO semester_terms (code, start_date, end_date)"
                " VALUES (:term, '2026-09-14', '2027-01-08') ON CONFLICT DO NOTHING"
            ),
            {"term": term},
        )
        for change_id, kind, note_zh, note_en in rows:
            await conn.execute(
                _INSERT,
                {"id": change_id, "term": term, "kind": kind, "note_zh": note_zh,
                 "note_en": note_en},
            )


async def _rows(engine: AsyncEngine) -> list[tuple]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(_ROWS)).all()]


def _sorted(rows) -> list[tuple]:
    return sorted(rows, key=lambda row: row[0])


async def test_upgrade_builds_the_constraints_and_function_the_model_declares(
    migration_db_url: str, db_session
) -> None:
    declared = set((await db_session.scalars(_SHAPE)).all())
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            before = set((await conn.scalars(_SHAPE)).all())
        assert before != declared

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        # A second run, after a rollback that kept them, changes nothing.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == before
    finally:
        await engine.dispose()


async def test_the_term_notes_are_converted_by_id_and_given_back(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        await _seed(engine, _NOTES)

        _alembic(migration_db_url, "upgrade", REVISION)
        assert await _rows(engine) == _sorted(_CONVERTED)

        _alembic(migration_db_url, "downgrade", BEFORE)
        assert await _rows(engine) == _sorted(_NOTES)
    finally:
        await engine.dispose()


async def test_other_rows_at_those_ids_stay_as_they_are(migration_db_url: str) -> None:
    others = [
        (14, "note", "期中考試", None),
        (16, "note", "期中簡報", None),
        (17, "cancelled", None, None),
        (23, "note", "期末考 ", "Final exam"),
        (35, "note", "改為線上上課", None),
        (54, "note", "期末簡報", "Final presentation"),
    ]
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO semester_terms (code, start_date, end_date)"
                    " VALUES ('1151', '2026-09-14', '2027-01-08')"
                )
            )
            for change_id, kind, note_zh, note_en in others:
                await conn.execute(
                    text(
                        "INSERT INTO course_changes (id, term_code, course_no, kind, mode,"
                        " start_date, end_date, room, note_zh, note_en, created_by, updated_by)"
                        " VALUES (:id, '1151', 'CS2001301', :kind, 'day', '2026-11-05',"
                        " '2026-11-05', :room, :note_zh, :note_en, 'ops', 'ops')"
                    ),
                    {"id": change_id, "kind": kind, "note_zh": note_zh, "note_en": note_en,
                     "room": "IB-101" if kind == "room" else None},
                )
        # The same texts in another term.
        await _seed(
            engine,
            [(33, "note", "期中考", "Midterm exam"), (36, "note", _ONLINE_TEXT, None)],
            term="1152",
        )

        _alembic(migration_db_url, "upgrade", REVISION)

        assert await _rows(engine) == _sorted(
            [*others, (33, "note", "期中考", "Midterm exam"), (36, "note", _ONLINE_TEXT, None)]
        )
    finally:
        await engine.dispose()


async def test_a_downgrade_turns_changes_added_since_into_notes(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        await _seed(engine, _NOTES)
        _alembic(migration_db_url, "upgrade", REVISION)
        await _seed(
            engine,
            [
                (100, "assessment", "小考", "Quiz"),
                (101, "online", None, None),
                (102, "online", "同步視訊", None),
            ],
        )

        _alembic(migration_db_url, "downgrade", BEFORE)

        assert await _rows(engine) == _sorted(
            [
                *_NOTES,
                (100, "note", "小考", "Quiz"),
                (101, "note", "線上", "Online"),
                (102, "note", "同步視訊", None),
            ]
        )
    finally:
        await engine.dispose()
