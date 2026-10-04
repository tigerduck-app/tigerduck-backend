"""Prometheus instrumentation: HTTP labels, scheduler outcomes, the DB pool
collector, push and LLM timing, and the separate /metrics endpoint.

Every metric lives in the process-wide default registry, which the rest of
the suite also feeds, so each test reads a sample before and after and
asserts on the difference rather than on an absolute value.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from aioapns.common import NotificationResult
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from server import __version__, metrics
from server.bulletins.llm.base import BulletinMetadata, LLMError
from server.bulletins.llm.openai_compat import (
    OpenAICompatibleProvider,
    RecordingProvider,
)
from server.bulletins.taxonomy import CanonicalOrg
from server.config import Settings
from server.db import build_engine
from server.push.apns_client import AioApnsSender, RecordingSender
from server.push.fcm_client import FcmSender, RecordingFcmSender
from server.push.payload import ApnsRequest, FcmRequest, PushKind
from server.push.router import PushRouter
from server.scheduler.runtime import build_scheduler


def _value(name: str, **labels: str) -> float:
    """One sample's current value; a series not created yet reads as 0."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- HTTP middleware --------------------------------------------------------


def _small_app() -> FastAPI:
    """Shaped like the real app where it matters: the routes sit on an
    APIRouter that is itself included under a prefix, which is what splits
    the router-local template from the one the client hit."""
    devices = APIRouter(prefix="/devices")

    @devices.get("/{device_id}")
    async def get_device(device_id: str) -> dict[str, str]:
        return {"device_id": device_id}

    @devices.get("/{device_id}/missing")
    async def missing(device_id: str) -> None:
        raise HTTPException(status_code=404)

    @devices.post("/{device_id}/boom")
    async def boom(device_id: str) -> None:
        raise RuntimeError("boom")

    @devices.get("/{device_id}/in-progress")
    async def in_progress(device_id: str) -> dict[str, float]:
        return {"in_progress": _value("tigerduck_http_requests_in_progress")}

    app = FastAPI()
    app.include_router(devices, prefix="/v3")
    app.add_middleware(metrics.HttpMetricsMiddleware)
    return app


def _small_client(app: FastAPI) -> AsyncClient:
    # raise_app_exceptions=False so an unhandled error comes back as the
    # 500 ServerErrorMiddleware answers it with, the way uvicorn serves it.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_path_param_route_is_labelled_with_its_prefixed_template():
    route = "/v3/devices/{device_id}"
    before = _value("tigerduck_http_requests_total", method="GET", route=route, status="200")
    before_timed = _value(
        "tigerduck_http_request_duration_seconds_count", method="GET", route=route
    )

    async with _small_client(_small_app()) as client:
        assert (await client.get("/v3/devices/abc")).status_code == 200
        assert (await client.get("/v3/devices/xyz")).status_code == 200

    # Both ids land on one series; neither id becomes a label value.
    assert _value(
        "tigerduck_http_requests_total", method="GET", route=route, status="200"
    ) == before + 2
    assert _value(
        "tigerduck_http_request_duration_seconds_count", method="GET", route=route
    ) == before_timed + 2
    assert REGISTRY.get_sample_value(
        "tigerduck_http_requests_total",
        {"method": "GET", "route": "/v3/devices/abc", "status": "200"},
    ) is None


async def test_path_that_matches_no_route_is_labelled_unmatched():
    before = _value(
        "tigerduck_http_requests_total", method="GET", route="unmatched", status="404"
    )

    async with _small_client(_small_app()) as client:
        assert (await client.get("/wp-login.php")).status_code == 404

    assert _value(
        "tigerduck_http_requests_total", method="GET", route="unmatched", status="404"
    ) == before + 1
    assert REGISTRY.get_sample_value(
        "tigerduck_http_requests_total",
        {"method": "GET", "route": "/wp-login.php", "status": "404"},
    ) is None


async def test_unknown_method_collapses_to_other():
    before = _value(
        "tigerduck_http_requests_total", method="OTHER", route="unmatched", status="404"
    )

    async with _small_client(_small_app()) as client:
        await client.request("PROPFIND", "/nothing-here")

    assert _value(
        "tigerduck_http_requests_total", method="OTHER", route="unmatched", status="404"
    ) == before + 1


async def test_matched_route_keeps_its_template_on_an_error_status():
    route = "/v3/devices/{device_id}/missing"
    before = _value("tigerduck_http_requests_total", method="GET", route=route, status="404")

    async with _small_client(_small_app()) as client:
        assert (await client.get("/v3/devices/abc/missing")).status_code == 404

    assert _value(
        "tigerduck_http_requests_total", method="GET", route=route, status="404"
    ) == before + 1


async def test_exception_before_a_response_counts_as_500():
    route = "/v3/devices/{device_id}/boom"
    before = _value("tigerduck_http_requests_total", method="POST", route=route, status="500")
    in_progress = _value("tigerduck_http_requests_in_progress")

    async with _small_client(_small_app()) as client:
        assert (await client.post("/v3/devices/abc/boom")).status_code == 500

    assert _value(
        "tigerduck_http_requests_total", method="POST", route=route, status="500"
    ) == before + 1
    # The failed request still left the in-progress gauge.
    assert _value("tigerduck_http_requests_in_progress") == in_progress


async def test_in_progress_gauge_counts_the_running_request():
    baseline = _value("tigerduck_http_requests_in_progress")

    async with _small_client(_small_app()) as client:
        response = await client.get("/v3/devices/abc/in-progress")

    assert response.json() == {"in_progress": baseline + 1}
    assert _value("tigerduck_http_requests_in_progress") == baseline


@pytest.mark.asyncio(loop_scope="session")
async def test_real_app_labels_its_routes(client: AsyncClient):
    """The real app: an unversioned route, a /v3 route with a path param,
    the 410 the legacy middleware answers without routing, and one of
    Starlette's own routes FastAPI adds for the docs."""
    expectations = [
        ("GET", "/health", "/health"),
        ("DELETE", "/v3/devices/some-device", "/v3/devices/{device_id}"),
        ("GET", "/v1/ping", "unmatched"),
        ("GET", "/openapi.json", "/openapi.json"),
    ]
    for method, path, route in expectations:
        response = await client.request(method, path)
        status = str(response.status_code)
        before = _value(
            "tigerduck_http_requests_total", method=method, route=route, status=status
        )
        response = await client.request(method, path)
        assert str(response.status_code) == status
        assert _value(
            "tigerduck_http_requests_total", method=method, route=route, status=status
        ) == before + 1, (method, path)

    # The public API port serves no metrics; only the separate port does.
    assert (await client.get("/metrics")).status_code == 404


def test_building_the_app_again_registers_nothing_twice(test_settings: Settings):
    # Module-level definitions are what keep this safe: a second import is
    # a cache hit, and create_app adds the middleware but no collectors.
    import server.main
    import server.metrics  # noqa: F401
    from server.main import create_app

    first = create_app(test_settings)
    second = create_app(test_settings)

    for app in (first, second, server.main.app):
        installed = [m.cls for m in app.user_middleware]
        assert installed.count(metrics.HttpMetricsMiddleware) == 1


# ---- Scheduler listener -----------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _submitted(job_id: str) -> JobSubmissionEvent:
    return JobSubmissionEvent(
        EVENT_JOB_SUBMITTED, job_id, "default", [datetime.now(timezone.utc)]
    )


def _execution(code: int, job_id: str) -> JobExecutionEvent:
    return JobExecutionEvent(code, job_id, "default", datetime.now(timezone.utc))


def _runs(job_id: str, outcome: str) -> float:
    return _value("tigerduck_scheduler_job_runs_total", job_id=job_id, outcome=outcome)


def test_listener_counts_success_and_error_and_times_each_run():
    job_id = "metrics_test_listener"
    clock = _Clock()
    listener = metrics.SchedulerJobMetrics(clock=clock)
    success, error = _runs(job_id, "success"), _runs(job_id, "error")
    timed = _value("tigerduck_scheduler_job_duration_seconds_count", job_id=job_id)
    total = _value("tigerduck_scheduler_job_duration_seconds_sum", job_id=job_id)

    clock.now = 10.0
    listener(_submitted(job_id))
    clock.now = 12.5
    listener(_execution(EVENT_JOB_EXECUTED, job_id))
    clock.now = 20.0
    listener(_submitted(job_id))
    clock.now = 21.0
    listener(_execution(EVENT_JOB_ERROR, job_id))

    assert _runs(job_id, "success") == success + 1
    assert _runs(job_id, "error") == error + 1
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_count", job_id=job_id
    ) == timed + 2
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_sum", job_id=job_id
    ) == pytest.approx(total + 2.5 + 1.0)


def test_listener_counts_missed_and_skipped_runs_without_timing_them():
    job_id = "metrics_test_not_run"
    listener = metrics.SchedulerJobMetrics()
    missed, skipped = _runs(job_id, "missed"), _runs(job_id, "skipped")
    timed = _value("tigerduck_scheduler_job_duration_seconds_count", job_id=job_id)

    listener(_execution(EVENT_JOB_MISSED, job_id))
    listener(
        JobSubmissionEvent(
            EVENT_JOB_MAX_INSTANCES, job_id, "default", [datetime.now(timezone.utc)]
        )
    )

    assert _runs(job_id, "missed") == missed + 1
    assert _runs(job_id, "skipped") == skipped + 1
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_count", job_id=job_id
    ) == timed


def test_listener_counts_a_run_it_never_saw_start_but_does_not_time_it():
    job_id = "metrics_test_no_start"
    listener = metrics.SchedulerJobMetrics()
    success = _runs(job_id, "success")
    timed = _value("tigerduck_scheduler_job_duration_seconds_count", job_id=job_id)

    listener(_execution(EVENT_JOB_EXECUTED, job_id))

    assert _runs(job_id, "success") == success + 1
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_count", job_id=job_id
    ) == timed


async def test_listener_sees_a_real_scheduler_run_jobs():
    ok_id, failing_id = "metrics_test_real_ok", "metrics_test_real_failing"

    async def ok_job() -> None:
        await asyncio.sleep(0.01)

    async def failing_job() -> None:
        raise RuntimeError("job failed")

    scheduler = AsyncIOScheduler(timezone="UTC")
    # No trigger: each job runs once, as soon as the scheduler starts.
    scheduler.add_job(ok_job, id=ok_id)
    scheduler.add_job(failing_job, id=failing_id)
    metrics.instrument_scheduler(scheduler)

    # Created at zero before anything ran, so `increase()` sees the first.
    assert REGISTRY.get_sample_value(
        "tigerduck_scheduler_job_runs_total", {"job_id": ok_id, "outcome": "error"}
    ) is not None
    success, error = _runs(ok_id, "success"), _runs(failing_id, "error")
    timed = _value("tigerduck_scheduler_job_duration_seconds_count", job_id=ok_id)

    scheduler.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            _runs(ok_id, "success") == success or _runs(failing_id, "error") == error
        ):
            await asyncio.sleep(0.01)
    finally:
        scheduler.shutdown(wait=False)

    assert _runs(ok_id, "success") == success + 1
    assert _runs(failing_id, "error") == error + 1
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_count", job_id=ok_id
    ) == timed + 1
    assert _value(
        "tigerduck_scheduler_job_duration_seconds_sum", job_id=ok_id
    ) >= 0.01


async def test_build_scheduler_instruments_every_job(test_settings: Settings):
    scheduler = build_scheduler(
        async_sessionmaker(),
        PushRouter(apple=RecordingSender(), android=RecordingFcmSender()),
        test_settings,
        llm=RecordingProvider(BulletinMetadata(canonical_org=CanonicalOrg.other)),
    )

    assert any(
        isinstance(callback, metrics.SchedulerJobMetrics)
        for callback, _mask in scheduler._listeners
    )
    for job in scheduler.get_jobs():
        for outcome in metrics.JOB_OUTCOMES:
            assert REGISTRY.get_sample_value(
                "tigerduck_scheduler_job_runs_total",
                {"job_id": job.id, "outcome": outcome},
            ) is not None, (job.id, outcome)


# ---- DB pool collector ------------------------------------------------------


def _pool(state: str) -> float | None:
    return REGISTRY.get_sample_value("tigerduck_db_pool_connections", {"state": state})


def test_pool_metrics_are_absent_until_an_engine_is_bound():
    metrics.bind_engine(None)
    assert REGISTRY.get_sample_value("tigerduck_db_pool_size") is None
    assert _pool("checked_out") is None


async def test_pool_metrics_read_an_idle_engine():
    # Nothing listens on port 1; the pool never connects until checked out.
    engine = build_engine(
        Settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/none")
    )
    metrics.bind_engine(engine)
    try:
        assert REGISTRY.get_sample_value("tigerduck_db_pool_size") == 5
        assert REGISTRY.get_sample_value("tigerduck_db_pool_max_overflow") == 5
        assert _pool("checked_out") == 0
        assert _pool("checked_in") == 0
        # QueuePool itself reports -5 here; the collector clamps it.
        assert _pool("overflow") == 0
    finally:
        metrics.bind_engine(None)
        await engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_pool_metrics_follow_live_checkouts(
    test_settings: Settings, prepared_engine: AsyncEngine
):
    # Its own engine (on the test database `prepared_engine` created), so
    # nothing else in the suite holds a connection from this pool.
    engine = build_engine(test_settings)
    metrics.bind_engine(engine)
    try:
        async with AsyncExitStack() as stack:
            # One past pool_size, to push the pool into overflow.
            for _ in range(6):
                conn = await stack.enter_async_context(engine.connect())
                await conn.execute(text("SELECT 1"))
            assert _pool("checked_out") == 6
            assert _pool("checked_in") == 0
            assert _pool("overflow") == 1

        # The pool keeps pool_size connections and closes the overflow one.
        assert _pool("checked_out") == 0
        assert _pool("checked_in") == 5
        assert _pool("overflow") == 0
    finally:
        metrics.bind_engine(None)
        await engine.dispose()


# ---- Push senders -----------------------------------------------------------


def _push(provider: str, outcome: str) -> float:
    return _value(
        "tigerduck_push_send_duration_seconds_count", provider=provider, outcome=outcome
    )


class _FakeApnsClient:
    def __init__(self, outcome: NotificationResult | Exception | str) -> None:
        self._outcome = outcome
        self.pool = SimpleNamespace(close=lambda: None)

    async def send_notification(self, notification) -> NotificationResult:
        if self._outcome == "hang":
            await asyncio.sleep(1)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _apns_sender(
    outcome: NotificationResult | Exception | str, timeout: float = 5.0
) -> AioApnsSender:
    # Skips __init__, which needs a real .p8 key to build the aioapns client.
    sender = AioApnsSender.__new__(AioApnsSender)
    sender._settings = SimpleNamespace(apns_send_timeout_seconds=timeout)
    sender._client = _FakeApnsClient(outcome)
    # A timed-out send replaces the client; give it a fake to replace with.
    sender._build_client = lambda: _FakeApnsClient(outcome)
    return sender


def _apns_request() -> ApnsRequest:
    return ApnsRequest(
        device_token="ab" * 32,
        topic="org.ntust.app.TigerDuck",
        priority=10,
        expiration=int(time.time()) + 60,
        message={"aps": {"alert": "hi"}},
        kind=PushKind.alert,
    )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (NotificationResult("id-1", "200"), "success"),
        (NotificationResult("id-2", "410", "Unregistered"), "failure"),
    ],
)
async def test_apns_send_is_timed_by_its_result(outcome, expected):
    before = _push("apns", expected)
    await _apns_sender(outcome).send(_apns_request())
    assert _push("apns", expected) == before + 1


async def test_apns_send_that_raises_is_timed_and_reraised():
    before = _push("apns", "failure")
    with pytest.raises(ConnectionError):
        await _apns_sender(ConnectionError("down")).send(_apns_request())
    assert _push("apns", "failure") == before + 1


async def test_apns_send_that_times_out_is_timed_as_a_timeout():
    """The sender answers a timeout with a TIMEOUT result, not an exception."""
    before = _push("apns", "timeout")
    result = await _apns_sender("hang", timeout=0.05).send(_apns_request())
    assert result.status == "TIMEOUT"
    assert _push("apns", "timeout") == before + 1


def _fcm_sender(timeout: float = 5.0) -> FcmSender:
    # Skips __init__, which needs a service-account file to build the app.
    sender = FcmSender.__new__(FcmSender)
    sender._app = None
    sender._send_timeout = timeout
    return sender


def _fcm_request(token: str = "fcm-token") -> FcmRequest:
    return FcmRequest(token=token, title="t", body="b", data={"k": "v"})


async def test_fcm_send_is_timed_by_its_result(monkeypatch):
    from firebase_admin import messaging

    success, failure = _push("fcm", "success"), _push("fcm", "failure")

    monkeypatch.setattr(messaging, "send", lambda msg, app=None: "msg-1")
    assert (await _fcm_sender().send(_fcm_request())).success

    def _raise(msg, app=None):
        raise RuntimeError("fcm down")

    monkeypatch.setattr(messaging, "send", _raise)
    assert not (await _fcm_sender().send(_fcm_request())).success

    assert _push("fcm", "success") == success + 1
    assert _push("fcm", "failure") == failure + 1


async def test_fcm_send_past_its_deadline_is_a_timeout(monkeypatch):
    from firebase_admin import messaging

    before = _push("fcm", "timeout")
    monkeypatch.setattr(messaging, "send", lambda msg, app=None: time.sleep(0.2))

    result = await _fcm_sender(timeout=0.01).send(_fcm_request())

    assert result.status == "TIMEOUT"
    assert _push("fcm", "timeout") == before + 1


async def test_fcm_batch_observes_each_message_with_its_own_outcome(monkeypatch):
    from firebase_admin import messaging

    success, failure = _push("fcm", "success"), _push("fcm", "failure")
    batch = SimpleNamespace(
        responses=[
            SimpleNamespace(success=True, message_id="m-1", exception=None),
            SimpleNamespace(success=True, message_id="m-2", exception=None),
            SimpleNamespace(
                success=False, message_id=None, exception=RuntimeError("bad")
            ),
        ]
    )
    monkeypatch.setattr(messaging, "send_each", lambda msgs, app=None: batch)

    results = await _fcm_sender().send_multi(
        [_fcm_request("a"), _fcm_request("b"), _fcm_request("c")]
    )

    assert [r.success for r in results] == [True, True, False]
    assert _push("fcm", "success") == success + 2
    assert _push("fcm", "failure") == failure + 1


async def test_recording_senders_are_not_timed():
    apns = _push("apns", "success")
    fcm = _push("fcm", "success")

    await RecordingSender().send(_apns_request())
    await RecordingFcmSender().send_multi([_fcm_request()])

    assert _push("apns", "success") == apns
    assert _push("fcm", "success") == fcm


# ---- LLM --------------------------------------------------------------------


def _llm(handler) -> tuple[OpenAICompatibleProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="http://mock/v1",
        api_key="sk-test",
        model="test-model",
        max_retries=0,
        client=client,
    )
    return provider, client


def _llm_requests(outcome: str) -> float:
    return _value("tigerduck_llm_request_duration_seconds_count", outcome=outcome)


async def test_llm_request_is_timed_as_success():
    content = {
        "canonical_org": CanonicalOrg.other.value,
        "content_tags": [],
        "title": "標題",
        "summary": "摘要",
        "body_clean": "內文",
        "importance": "normal",
    }
    provider, client = _llm(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )
    )
    before = _llm_requests("success")
    try:
        await provider.classify(title="t", raw_publisher="p", body_md="b")
    finally:
        await client.aclose()
    assert _llm_requests("success") == before + 1


async def test_llm_request_that_fails_is_timed_as_error():
    provider, client = _llm(lambda request: httpx.Response(503))
    before = _llm_requests("error")
    try:
        with pytest.raises(LLMError):
            await provider.classify(title="t", raw_publisher="p", body_md="b")
    finally:
        await client.aclose()
    assert _llm_requests("error") == before + 1


# ---- /metrics endpoint ------------------------------------------------------


def test_port_zero_serves_no_metrics():
    assert metrics.start_metrics_server(0) is None


def test_metrics_server_serves_the_default_registry():
    port = _free_port()
    server = metrics.start_metrics_server(port)
    assert server is not None
    try:
        body = httpx.get(f"http://127.0.0.1:{port}/metrics").text
    finally:
        metrics.stop_metrics_server(server)

    assert f'tigerduck_build_info{{version="{__version__}"}} 1.0' in body
    assert "# TYPE tigerduck_http_requests_total counter" in body
    # The `_created` companion series are switched off.
    assert "tigerduck_http_requests_created" not in body
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/metrics")


def test_a_port_already_taken_is_logged_not_raised():
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        assert metrics.start_metrics_server(taken.getsockname()[1]) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_serves_metrics_only_while_the_app_runs(
    test_settings: Settings, prepared_engine: AsyncEngine
):
    from server.main import create_app, lifespan

    port = _free_port()
    app = create_app(
        test_settings.model_copy(update={"skip_llm_probe": True, "metrics_port": port})
    )
    url = f"http://127.0.0.1:{port}/metrics"

    async with lifespan(app):
        async with httpx.AsyncClient() as scraper:
            body = (await scraper.get(url)).text
        assert "tigerduck_db_pool_size 5.0" in body
        assert (
            'tigerduck_scheduler_job_runs_total{job_id="bulletin_scrape",'
            'outcome="error"} 0.0'
        ) in body

    with pytest.raises(httpx.ConnectError):
        httpx.get(url)
    assert REGISTRY.get_sample_value("tigerduck_db_pool_size") is None
