"""`SessionDep` commits before the response is sent."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.db import SessionDep


class _CommitFails:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def commit(self):
        raise RuntimeError("commit failed")

    async def rollback(self):
        pass


async def test_a_write_that_fails_to_commit_is_not_answered_as_a_success():
    app = FastAPI()
    app.state.session_factory = _CommitFails

    @app.post("/write")
    async def write(session: SessionDep) -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/write")

    assert resp.status_code == 500
