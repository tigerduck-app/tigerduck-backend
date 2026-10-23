"""Restore dumps the live database first and refuses to restore without it."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.routes import backup

pytestmark = pytest.mark.anyio


class _Proc:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


def _fake_tools(calls: list[str], pg_dump_ok: bool, pg_restore_ok: bool):
    async def create_subprocess_exec(program, *args, **kwargs):
        calls.append(program)
        if program == "pg_dump":
            if not pg_dump_ok:
                return _Proc(1, b"connection refused")
            kwargs["stdout"].write(b"PGDMP live")
            return _Proc(0)
        return _Proc(0) if pg_restore_ok else _Proc(1, b"bad archive")

    return create_subprocess_exec


async def _import(
    monkeypatch, tmp_path: Path, pg_dump_ok: bool, pg_restore_ok: bool = True
):
    calls: list[str] = []
    monkeypatch.setattr(backup, "PRE_RESTORE_DIR", tmp_path / "pre-restore")
    monkeypatch.setattr(
        backup.asyncio,
        "create_subprocess_exec",
        _fake_tools(calls, pg_dump_ok, pg_restore_ok),
    )
    app = FastAPI()
    app.state.settings = Settings.model_construct(
        database_url="postgresql+asyncpg://tigerduck:pw@tigerduck-db:5432/tigerduck"
    )
    app.include_router(backup.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as client:
        r = await client.post(
            "/api/backup/import", files={"file": ("x.dump", b"PGDMP upload")}
        )
    return r, calls


async def test_restore_takes_a_dump_of_the_live_database_first(
    monkeypatch, tmp_path: Path
) -> None:
    r, calls = await _import(monkeypatch, tmp_path, pg_dump_ok=True)

    assert r.status_code == 200
    assert calls == ["pg_dump", "pg_restore"]
    saved = tmp_path / "pre-restore" / r.json()["pre_restore_dump"]
    assert saved.read_bytes() == b"PGDMP live"
    assert saved.stat().st_mode & 0o777 == 0o600


async def test_only_the_newest_pre_restore_dumps_are_kept(
    monkeypatch, tmp_path: Path
) -> None:
    kept_dir = tmp_path / "pre-restore"
    kept_dir.mkdir()
    for day in range(1, 7):
        (kept_dir / f"pre-restore-2026010{day}T000000Z.dump").write_bytes(b"old")

    r, _ = await _import(monkeypatch, tmp_path, pg_dump_ok=True)

    assert r.status_code == 200
    names = sorted(p.name for p in kept_dir.iterdir())
    assert len(names) == backup.PRE_RESTORE_KEEP
    assert names[-1] == r.json()["pre_restore_dump"]
    assert "pre-restore-20260102T000000Z.dump" not in names


async def test_a_failed_restore_names_the_pre_restore_dump(
    monkeypatch, tmp_path: Path
) -> None:
    r, calls = await _import(monkeypatch, tmp_path, pg_dump_ok=True, pg_restore_ok=False)

    assert r.status_code == 500
    assert calls == ["pg_dump", "pg_restore"]
    (saved,) = (tmp_path / "pre-restore").iterdir()
    assert saved.name in r.json()["detail"]


async def test_restore_is_refused_when_the_pre_restore_dump_fails(
    monkeypatch, tmp_path: Path
) -> None:
    r, calls = await _import(monkeypatch, tmp_path, pg_dump_ok=False)

    assert r.status_code == 500
    assert "nothing was restored" in r.json()["detail"]
    assert calls == ["pg_dump"]
    assert list((tmp_path / "pre-restore").iterdir()) == []


async def test_dumps_taken_in_the_same_second_get_files_of_their_own(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(backup, "PRE_RESTORE_DIR", tmp_path / "pre-restore")
    monkeypatch.setattr(
        backup.asyncio,
        "create_subprocess_exec",
        _fake_tools(calls, pg_dump_ok=True, pg_restore_ok=True),
    )
    url = "postgresql+asyncpg://tigerduck:pw@tigerduck-db:5432/tigerduck"

    first = await backup._pre_restore_dump(url)
    second = await backup._pre_restore_dump(url)

    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"PGDMP live"


async def test_a_second_restore_is_refused_while_one_runs(
    monkeypatch, tmp_path: Path
) -> None:
    async with backup._RESTORE_LOCK:
        r, calls = await _import(monkeypatch, tmp_path, pg_dump_ok=True)

    assert r.status_code == 409
    assert calls == []
