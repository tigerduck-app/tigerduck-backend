"""Pooled sessions: when `SessionDep` commits, and the server-side timeouts."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from server.db import SessionDep, lift_timeouts


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


@pytest.mark.asyncio(loop_scope="session")
async def test_pooled_connections_carry_the_server_side_timeouts(db_session):
    assert await db_session.scalar(text("SHOW statement_timeout")) == "30s"
    assert (
        await db_session.scalar(text("SHOW idle_in_transaction_session_timeout"))
        == "1min"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_lifted_timeouts_last_one_transaction(db_session):
    await lift_timeouts(db_session)
    assert await db_session.scalar(text("SHOW statement_timeout")) == "0"
    assert await db_session.scalar(text("SHOW idle_in_transaction_session_timeout")) == "0"
    await db_session.commit()

    assert await db_session.scalar(text("SHOW statement_timeout")) == "30s"


@pytest.mark.asyncio(loop_scope="session")
async def test_scheduled_work_draws_on_a_pool_of_its_own(test_settings, db_session):
    from server.main import create_app, lifespan

    app = create_app(test_settings.model_copy(update={"skip_llm_probe": True}))
    async with lifespan(app):
        assert app.state.push_worker.session_factory.kw["bind"] is not app.state.engine
