"""A student ID an operator types reaches the account in any case and with
stray spaces, as login keys it (tigerduck-backend#93). In the server suite
for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.custom_push import _PreviewRequest, preview  # noqa: E402
from portal.app.routes.deregister import (  # noqa: E402
    DeregisterRequest,
    deregister_student,
    preview_student,
)
from portal.app.routes.moodle import manual_tests  # noqa: E402
from portal.app.routes.moodle import push as push_routes  # noqa: E402
from portal.app.routes.moodle.inspection import (  # noqa: E402
    student_timetable,
    sync_courses,
    sync_events,
)
from portal.app.routes.moodle.logs import sync_logs  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

TYPED = " b99900001 "


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(actor="ops@example.com"), url=SimpleNamespace(path="/api/x")
    )


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session, monkeypatch):
    async def no_tick(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(push_routes, "run_pipeline_now", no_tick)
    monkeypatch.setattr(manual_tests, "run_pipeline_now", no_tick)
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


async def _student(pool) -> tuple:
    user_id = await pool.fetchval(
        "INSERT INTO users (student_id, display_name) VALUES ('B99900001', '王小明') RETURNING id"
    )
    device_id = await pool.fetchval(
        "INSERT INTO user_devices (user_id, client_device_id, platform, device_class)"
        " VALUES ($1, 'iphone-1', 'ios', 'iphone') RETURNING id",
        user_id,
    )
    return user_id, device_id


async def _viewed(pool, user_id, _response) -> bool:
    return await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM admin_audit_log WHERE target_user_id = $1)", user_id
    )


async def _deleted(pool, user_id, _response) -> bool:
    return not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM users WHERE id = $1)", user_id)


@pytest.mark.parametrize(
    ("call", "found"),
    [
        (lambda p: sync_logs(_request(), student_id=TYPED, after_id=0, limit=10, pool=p), _viewed),
        (lambda p: push_routes.clear_push_queue(_request(), student_id=TYPED, pool=p), _viewed),
        (
            lambda p: push_routes.force_sync_trigger(student_id=TYPED, pool=p, request=_request()),
            _viewed,
        ),
        (
            lambda p: manual_tests.send_test_reauth_push(_request(), student_id=TYPED, pool=p),
            _viewed,
        ),
        (lambda p: student_timetable(_request(), student_id=TYPED, term=None, pool=p), _viewed),
        (lambda p: sync_events(_request(), student_id=TYPED, limit=10, pool=p), _viewed),
        (lambda p: sync_courses(_request(), student_id=TYPED, pool=p), _viewed),
        (lambda p: preview_student(_request(), pool=p, student_id=TYPED), _viewed),
        (
            lambda p: deregister_student(DeregisterRequest(student_id=TYPED), _request(), pool=p),
            _deleted,
        ),
    ],
    ids=[
        "sync logs",
        "clear push queue",
        "sync trigger",
        "reauth push",
        "timetable",
        "sync events",
        "sync courses",
        "deregister preview",
        "deregister",
    ],
)
async def test_a_typed_student_id_finds_the_account(pool, call, found) -> None:
    user_id, _ = await _student(pool)
    response = await call(pool)
    assert await found(pool, user_id, response)


async def test_a_test_live_activity_finds_the_device(pool) -> None:
    _, device_id = await _student(pool)
    body = manual_tests._LiveActivityTest(student_id=TYPED, device_id=str(device_id), title="OS")
    response = await manual_tests.start_test_live_activity(body, _request(), pool=pool)
    # It has no push-to-start token; a 404 would mean the student was not found.
    assert response.status_code == 409


async def test_a_live_activity_end_now_finds_the_job(pool) -> None:
    user_id, device_id = await _student(pool)
    job_id = await pool.fetchval(
        "INSERT INTO push_jobs (user_id, device_id, dedupe_key, channel, scenario, fire_at,"
        " payload, status) VALUES ($1, $2, 'la_end:x', 'schedule', 'inClass',"
        " now() + interval '1 hour', '{\"kind\": \"live_activity_end\"}'::jsonb, 'pending')"
        " RETURNING id",
        user_id,
        device_id,
    )
    response = await manual_tests.end_live_activity_now(
        _request(), student_id=TYPED, job_id=job_id, pool=pool
    )
    assert response.status_code == 200


async def test_a_custom_push_to_a_typed_student_id_reaches_the_student(pool) -> None:
    _, device_id = await _student(pool)
    await pool.execute(
        "INSERT INTO device_push_tokens (device_id, provider, token_kind, token_hash,"
        " token_value, status) VALUES ($1, 'apns', 'standard', 'h', 'tok', 'active')",
        device_id,
    )
    response = await preview(_PreviewRequest(target_classes=["iphone"], user_id=TYPED), pool=pool)
    assert json.loads(response.body)["matched"]["total"] == 1
