"""Prometheus metrics: definitions, the HTTP middleware, the scheduler
listener, the DB pool collector and the scrape endpoint.

Everything registers on prometheus_client's default registry, once, when
this module is first imported. uvicorn runs a single worker (APScheduler
lives in the FastAPI lifespan, see entrypoint.sh), so that one registry
sees every request and every job and multiprocess mode is not needed.
Because the definitions live at module level, building the app again --
the test suite does it per test -- never registers anything twice.

The metrics are served on their own port by `start_metrics_server`, never
by the FastAPI app: :40000 is what nginx-proxy-manager forwards to the
internet, while the metrics port is only reachable on the internal docker
network Prometheus scrapes over.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from wsgiref.simple_server import WSGIServer

import structlog
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobEvent,
)
from apscheduler.schedulers.base import BaseScheduler
from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server import __version__

logger = structlog.get_logger(__name__)

# Every counter and histogram would otherwise carry a `_created` series per
# label set. No dashboard reads them, and they double what Prometheus
# stores for the HTTP metrics, whose label sets are the most numerous.
disable_created_metrics()


BUILD_INFO = Gauge(
    "tigerduck_build_info",
    "Always 1; the version label says which backend build is running.",
    ["version"],
)
BUILD_INFO.labels(version=__version__).set(1)


# --- HTTP ---

# Requests that matched no route: 404 scans, the /v1 + /v2 410 answers,
# trailing-slash redirects. Labelling them by raw path would hand anyone
# probing the public API the power to mint new series.
UNMATCHED_ROUTE = "unmatched"

# A client can send any token as the method, so anything else collapses
# into one value for the same reason.
_KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

HTTP_REQUESTS = Counter(
    "tigerduck_http_requests_total",
    "HTTP requests answered, by method, matched route template and status.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "tigerduck_http_request_duration_seconds",
    "Time from receiving a request to finishing its response.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "tigerduck_http_requests_in_progress",
    "HTTP requests currently being handled.",
)


def route_template(scope: Scope) -> str:
    """The template of the route a finished request matched, or
    `UNMATCHED_ROUTE` if it matched none. Read it after the app has run:
    the router writes the match into the scope as it dispatches."""
    route = scope.get("route")
    if route is not None:
        # FastAPI no longer flattens an included router into the app, so
        # `scope["route"]` is the router's own APIRoute and its template
        # lacks the include prefix ("/devices/{device_id}", not
        # "/v3/devices/{device_id}"). The prefixed template is on the
        # effective route context FastAPI keeps under `scope["fastapi"]`.
        # A FastAPI that flattens has no such context, and the route's own
        # template is already the full one there.
        fastapi_scope = scope.get("fastapi")
        context = (
            fastapi_scope.get("effective_route_context")
            if isinstance(fastapi_scope, dict)
            else None
        )
        if context is not None and getattr(context, "original_route", None) is route:
            template = getattr(context, "path_format", None)
            if template:
                return template
        template = getattr(route, "path_format", None)
        if template:
            return template
    # Starlette's own routes (FastAPI's /docs, /openapi.json) set the
    # endpoint they matched but not `route`. One without path parameters
    # matched its path exactly, so that path is the template and cannot
    # carry anything the client chose.
    if "endpoint" in scope and not scope.get("path_params"):
        return scope["path"]
    return UNMATCHED_ROUTE


class HttpMetricsMiddleware:
    """Counts and times every HTTP request.

    Plain ASGI rather than `BaseHTTPMiddleware`, which runs the app in a
    separate task and breaks streaming responses and background tasks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Stays 500 if the app raises before it starts a response; that
        # exception reaches ServerErrorMiddleware, outside this one, which
        # answers it with a 500.
        status = 500

        async def send_and_capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        HTTP_REQUESTS_IN_PROGRESS.inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_and_capture_status)
        finally:
            elapsed = time.perf_counter() - started
            HTTP_REQUESTS_IN_PROGRESS.dec()
            method = scope["method"] if scope["method"] in _KNOWN_METHODS else "OTHER"
            route = route_template(scope)
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
            HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(elapsed)


# --- Scheduler ---

JOB_OUTCOMES = ("success", "error", "missed", "skipped")

# The label is `job_id`, not `job`: Prometheus sets `job` itself, to the
# scrape target's name, and would rename ours to `exported_job`.
SCHEDULER_JOB_RUNS = Counter(
    "tigerduck_scheduler_job_runs_total",
    "Scheduler job runs by outcome: success, error, missed (past its "
    "misfire grace time) or skipped (the previous run was still going).",
    ["job_id", "outcome"],
)
SCHEDULER_JOB_DURATION = Histogram(
    "tigerduck_scheduler_job_duration_seconds",
    "Time from a job's submission to it finishing, successfully or not.",
    ["job_id"],
    # Ticks usually finish in milliseconds; an LLM classify round or a
    # long sync can take minutes.
    buckets=(
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120,
        300, 600,
    ),
)

_SCHEDULER_EVENT_MASK = (
    EVENT_JOB_SUBMITTED
    | EVENT_JOB_EXECUTED
    | EVENT_JOB_ERROR
    | EVENT_JOB_MISSED
    | EVENT_JOB_MAX_INSTANCES
)


class SchedulerJobMetrics:
    """APScheduler listener that counts job outcomes and times each run.

    A run is timed from its EVENT_JOB_SUBMITTED to its EXECUTED or ERROR,
    keyed by job id. That pairing holds because every job is added with
    `max_instances=1`: a job cannot be submitted again while it runs, and
    the attempt is reported as EVENT_JOB_MAX_INSTANCES instead.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: dict[str, float] = {}

    def __call__(self, event: JobEvent) -> None:
        job_id = event.job_id
        if event.code == EVENT_JOB_SUBMITTED:
            self._started[job_id] = self._clock()
            return
        if event.code in (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR):
            outcome = "success" if event.code == EVENT_JOB_EXECUTED else "error"
            started = self._started.pop(job_id, None)
            # A run already going when the listener was added has no
            # start time; count it but don't invent a duration.
            if started is not None:
                SCHEDULER_JOB_DURATION.labels(job_id=job_id).observe(
                    self._clock() - started
                )
        elif event.code == EVENT_JOB_MISSED:
            outcome = "missed"
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            outcome = "skipped"
        else:
            return
        SCHEDULER_JOB_RUNS.labels(job_id=job_id, outcome=outcome).inc()


def instrument_scheduler(scheduler: BaseScheduler) -> None:
    """Attach the job metrics listener to `scheduler`.

    Every job already added gets its series created at zero first. A
    series that only appears on its first increment hides that increment
    from `increase()`, so the first error a job ever has would not show up
    on an error-rate panel.
    """
    for job in scheduler.get_jobs():
        for outcome in JOB_OUTCOMES:
            SCHEDULER_JOB_RUNS.labels(job_id=job.id, outcome=outcome)
        SCHEDULER_JOB_DURATION.labels(job_id=job.id)
    scheduler.add_listener(SchedulerJobMetrics(), _SCHEDULER_EVENT_MASK)


# --- Database pool ---


class _DbPoolCollector(Collector):
    """Reports the bound engine's connection pool as it is at scrape time.

    The engine is built in the lifespan, long after this module registers
    the collector, so it is bound late with `bind_engine`. Until then a
    scrape reports no pool samples.
    """

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None

    def describe(self) -> Iterable[GaugeMetricFamily]:
        # Names only, for the registry's duplicate check; without this it
        # would call `collect` at registration, before any engine exists.
        return self._families()

    def collect(self) -> Iterable[GaugeMetricFamily]:
        engine = self.engine
        if engine is None:
            return []
        # Read `engine.pool` on every scrape rather than holding the pool:
        # `dispose()` swaps a fresh pool in.
        pool = engine.pool
        if not isinstance(pool, QueuePool):
            return []
        connections, size, max_overflow = self._families()
        connections.add_metric(["checked_out"], pool.checkedout())
        connections.add_metric(["checked_in"], pool.checkedin())
        # QueuePool counts overflow from -pool_size, so it reads negative
        # until the pool is full; only the connections beyond pool_size
        # are overflow in the sense a dashboard means.
        connections.add_metric(["overflow"], max(0, pool.overflow()))
        size.add_metric([], pool.size())
        # QueuePool has no public accessor for max_overflow.
        max_overflow.add_metric([], getattr(pool, "_max_overflow", 0))
        return [connections, size, max_overflow]

    @staticmethod
    def _families() -> tuple[GaugeMetricFamily, GaugeMetricFamily, GaugeMetricFamily]:
        return (
            GaugeMetricFamily(
                "tigerduck_db_pool_connections",
                "Database pool connections: checked out by a session, idle "
                "in the pool, and open beyond pool_size (overflow).",
                labels=["state"],
            ),
            GaugeMetricFamily(
                "tigerduck_db_pool_size",
                "Connections the database pool keeps open (pool_size).",
            ),
            GaugeMetricFamily(
                "tigerduck_db_pool_max_overflow",
                "Connections the database pool may open beyond pool_size.",
            ),
        )


_db_pool_collector = _DbPoolCollector()
REGISTRY.register(_db_pool_collector)


def bind_engine(engine: AsyncEngine | None) -> None:
    """Point the pool metrics at `engine`; None stops reporting them."""
    _db_pool_collector.engine = engine


# --- Push providers ---

PUSH_SEND_DURATION = Histogram(
    "tigerduck_push_send_duration_seconds",
    "Time for a send to APNs or FCM to come back, by provider and outcome. "
    "One observation per message; a message in an FCM batch is observed "
    "with the whole batch's time.",
    ["provider", "outcome"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30),
)


def push_send_outcome(success: bool, status: str | None) -> str:
    """Map a sender's `SendResult` onto the push histogram's outcome."""
    if success:
        return "success"
    # FcmSender reports its own send deadline as this status rather than
    # raising, so the call site cannot tell a timeout apart otherwise.
    if status == "TIMEOUT":
        return "timeout"
    return "failure"


def observe_push_send(provider: str, outcome: str, seconds: float) -> None:
    PUSH_SEND_DURATION.labels(provider=provider, outcome=outcome).observe(seconds)


# --- LLM ---

LLM_REQUEST_DURATION = Histogram(
    "tigerduck_llm_request_duration_seconds",
    "Time for one classification request to the OpenAI-compatible LLM "
    "endpoint; each retry is its own request. error covers transport "
    "errors, timeouts, HTTP errors and responses that did not parse.",
    ["outcome"],
    # A local model on Apple Silicon takes seconds to tens of seconds per
    # bulletin; the client times out at llm_timeout_seconds (120s).
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180),
)


def observe_llm_request(outcome: str, seconds: float) -> None:
    LLM_REQUEST_DURATION.labels(outcome=outcome).observe(seconds)


# --- Exposition ---


def start_metrics_server(port: int) -> WSGIServer | None:
    """Serve /metrics on `port` from a daemon thread; 0 serves nothing.

    Binds every interface: in production the container is only reachable
    on its docker networks, and the port is not published to the host.

    A port that cannot be bound is logged and skipped rather than raised.
    The API matters more than its metrics, and a target Prometheus cannot
    scrape already shows up as down.
    """
    if port == 0:
        return None
    try:
        server, _thread = start_http_server(port, addr="0.0.0.0")
    except OSError as exc:
        logger.error("metrics.server_failed", port=port, error=str(exc))
        return None
    logger.info("metrics.serving", port=port)
    return server


def stop_metrics_server(server: WSGIServer | None) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()
