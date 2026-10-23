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


def _fake_tools(calls: list[str], pg_dump_ok: bool):
    async def create_subprocess_exec(program, *args, **kwargs):
        calls.append(program)
        if program == "pg_dump":
            if not pg_dump_ok:
                return _Proc(1, b"connection refused")
            kwargs["stdout"].write(b"PGDMP live")
            return _Proc(0)
        return _Proc(0)

    return create_subprocess_exec


async def _import(monkeypatch, tmp_path: Path, pg_dump_ok: bool):
    calls: list[str] = []
    monkeypatch.setattr(backup, "PRE_RESTORE_DIR", tmp_path / "pre-restore")
    monkeypatch.setattr(
        backup.asyncio, "create_subprocess_exec", _fake_tools(calls, pg_dump_ok)
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


async def test_restore_is_refused_when_the_pre_restore_dump_fails(
    monkeypatch, tmp_path: Path
) -> None:
    r, calls = await _import(monkeypatch, tmp_path, pg_dump_ok=False)

    assert r.status_code == 500
    assert "nothing was restored" in r.json()["detail"]
    assert calls == ["pg_dump"]
    assert list((tmp_path / "pre-restore").iterdir()) == []
