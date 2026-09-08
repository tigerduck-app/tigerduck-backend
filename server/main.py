"""FastAPI entrypoint for the push notification server."""

from __future__ import annotations

# Relax OpenSSL 3 strict cert parsing before any httpx client loads — see
# server/_ssl_compat.py for the full why.
from server import _ssl_compat  # noqa: F401, E402

import asyncio  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import structlog  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402

from server.security import require_shared_secret  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from server import __version__
from server.auth.crypto import CredentialCipher, CredentialCipherError
from server.auth.moodle import HttpMoodleVerifier
from server.auth.rate_limit import SlidingWindowLimiter
from server.config import Settings, get_settings
from server.db import build_engine, build_session_factory, session_scope
from server.logging_setup import configure as configure_logging
from server.system_settings import SystemSetting as _SystemSetting  # noqa: F401 — register model
# noqa: F401 — imported for the side effect of registering the tables with
# Base.metadata, which is what create_all in the test fixtures walks.
from server.academic_calendar.models import (  # noqa: F401
    AcademicHoliday as _AcademicHoliday,
    SemesterTerm as _SemesterTerm,
    UserHolidayOverride as _UserHolidayOverride,
)
from server.push.router import build_router
from server.routes import academics as academics_routes
from server.routes import auth as auth_routes
from server.routes import academic_calendar as academic_calendar_routes
from server.routes import bulletins_feed as bulletins_feed_routes
from server.routes import holiday_overrides as holiday_overrides_routes
from server.routes import bulletins_v3 as bulletins_v3_routes
from server.routes import live_activities_v3 as live_activities_v3_routes
from server.routes import schedule_v3 as schedule_v3_routes
from server.routes import settings_docs as settings_docs_routes
from server.routes import overrides as overrides_routes
from server.routes import sync as sync_routes
from server.routes import sync_jobs as sync_jobs_routes
from server.routes import user_devices as user_devices_routes
from server.push.pipeline import PushPipelineWorker
from server.scheduler.runtime import build_scheduler
from server.syncjobs.executor import SyncWorker, default_worker_id
from server.syncjobs.moodle_client import HttpAssignmentFetcher, HttpCourseFetcher
from server.syncjobs.policies import ensure_default_policies

logger = structlog.get_logger(__name__)


# How long startup is willing to wait for the LLM endpoint. 60s comfortably
# covers a cold `llama-server` load of Gemma-4 E4B Q4 on Apple Silicon.
_LLM_READY_WAIT_SECONDS = 60.0
_LLM_READY_POLL_INTERVAL = 2.0


async def _wait_for_llm(settings: Settings) -> bool:
    """Poll the LLM /models endpoint until 200 or the wait budget runs out.

    Returns True if the LLM answered in time, False otherwise.

    Intentionally NON-blocking: on failure we log a warning and let the
    server finish booting. Rationale:

    * Read endpoints (`GET /v3/bulletins/...`) don't need the LLM at all.
    * The scheduler's `bulletin_process` job has its own retry/backoff,
      so transient LLM downtime self-heals without server restart.
    * launchd / Docker supervisor would otherwise pin-pong the API
      process if we hard-failed startup when llama-server is slow to load.
    """
    base = settings.llm_base_url.rstrip("/")
    url = f"{base}/models"
    auth = {"Authorization": f"Bearer {settings.llm_api_key}"}
    deadline = time.monotonic() + _LLM_READY_WAIT_SECONDS
    attempt = 0
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.monotonic() < deadline:
            attempt += 1
            try:
                r = await client.get(url, headers=auth)
                if r.status_code < 400:
                    logger.info(
                        "llm.ready",
                        base_url=settings.llm_base_url,
                        attempt=attempt,
                    )
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_LLM_READY_POLL_INTERVAL)
    logger.warning(
        "llm.not_ready_after_wait",
        base_url=settings.llm_base_url,
        waited_seconds=_LLM_READY_WAIT_SECONDS,
        attempts=attempt,
    )
    return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "server.startup",
        env=settings.env,
        api_base_path=settings.api_v3_base_path,
        apns_env=settings.apns_env,
        apns_topic=settings.apns_topic_live_activity,
    )

    if settings.skip_llm_probe:
        logger.info("llm.skipped", base_url=settings.llm_base_url)
    else:
        await _wait_for_llm(settings)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    async with session_scope(session_factory) as seed_session:
        await ensure_default_policies(seed_session)
    router = build_router(settings)
    cipher = app.state.credential_cipher
    sync_worker = None
    if cipher is not None:
        sync_worker = SyncWorker(
            session_factory=session_factory,
            settings=settings,
            cipher=cipher,
            fetcher=HttpAssignmentFetcher(
                base_url=settings.moodle_base_url,
                timeout_seconds=settings.moodle_fetch_timeout_seconds,
            ),
            course_fetcher=HttpCourseFetcher(
                base_url=settings.moodle_base_url,
                timeout_seconds=settings.moodle_fetch_timeout_seconds,
            ),
            worker_id=default_worker_id(),
        )
    else:
        logger.warning("syncjobs.disabled_no_credential_keys")
    push_worker = PushPipelineWorker(
        session_factory=session_factory,
        settings=settings,
        router=router,
        worker_id=default_worker_id(),
    )
    scheduler = build_scheduler(
        session_factory,
        router,
        settings,
        sync_worker=sync_worker,
        push_worker=push_worker,
    )

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.router = router
    app.state.push_worker = push_worker
    # Keep the legacy `sender` attribute pointing at the APNs sender so any
    # tooling that read `app.state.sender` for Live-Activity / iOS paths
    # keeps working without a downstream change.
    app.state.sender = router.apple
    app.state.scheduler = scheduler
    app.state.settings = settings

    scheduler.start()
    logger.info("scheduler.started", tick_seconds=settings.scheduler_tick_seconds)

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await router.close()
        await engine.dispose()
        logger.info("server.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="TigerDuck Push Server",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Legacy /v1 and /v2 are retired. Answer any request to them with
    # 410 Gone (not a bare 404) so an out-of-date client gets an
    # unambiguous "this API version is removed — update the app" signal.
    @app.middleware("http")
    async def _legacy_api_gone(request, call_next):
        path = request.url.path
        if path in ("/v1", "/v2") or path.startswith("/v1/") or path.startswith("/v2/"):
            return JSONResponse(
                status_code=410,
                content={
                    "detail": (
                        "This API version has been retired. Update the app "
                        "to the latest version."
                    ),
                    "current_api": settings.api_v3_base_path,
                },
            )
        return await call_next(request)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.env}

    @app.get("/version", tags=["meta"])
    async def version() -> dict[str, str]:
        # Unversioned on purpose — operator tooling (portal status, start.sh)
        # shouldn't have to know the prefix to ask what's running. Reports the
        # live v3 base path (v1/v2 are retired → 410).
        return {"version": __version__, "api_base_path": settings.api_v3_base_path}

    # Unversioned (was {api_base_path}/ping) so it survives the /v1+/v2
    # sunset and infra probes don't need to know a version prefix.
    @app.get("/ping", tags=["meta"])
    async def ping() -> dict[str, str]:
        return {"pong": "tigerduck"}

    @app.post("/push-tick", tags=["meta"], dependencies=[Depends(require_shared_secret)])
    async def force_push_tick() -> dict:
        worker = getattr(app.state, "push_worker", None)
        if worker is None:
            return {"ok": False, "error": "push_worker not available"}
        from server.push.pipeline import run_push_tick
        await run_push_tick(worker)
        return {"ok": True}

    # /v3 collaborators live on app.state (not lifespan) so tests can swap
    # them before issuing requests. The cipher is None when credential keys
    # are unconfigured — /v3 auth then answers 503 auth_not_configured.
    app.state.moodle_verifier = HttpMoodleVerifier(
        base_url=settings.moodle_base_url,
        timeout_seconds=settings.moodle_verify_timeout_seconds,
    )
    app.state.login_limiter = SlidingWindowLimiter(
        max_attempts=settings.auth_login_max_attempts,
        window_seconds=settings.auth_login_window_seconds,
    )
    from server.routes.auth import (
        _CREDENTIALS_MAX_ATTEMPTS,
        _CREDENTIALS_WINDOW_SECONDS,
        _REFRESH_MAX_ATTEMPTS,
        _REFRESH_WINDOW_SECONDS,
    )
    app.state.refresh_limiter = SlidingWindowLimiter(
        max_attempts=_REFRESH_MAX_ATTEMPTS,
        window_seconds=_REFRESH_WINDOW_SECONDS,
    )
    app.state.credentials_limiter = SlidingWindowLimiter(
        max_attempts=_CREDENTIALS_MAX_ATTEMPTS,
        window_seconds=_CREDENTIALS_WINDOW_SECONDS,
    )
    from server.routes.user_devices import (
        _ANON_DEVICE_MAX_ATTEMPTS,
        _ANON_DEVICE_WINDOW_SECONDS,
        _ANON_IP_MAX_ATTEMPTS,
        _ANON_IP_WINDOW_SECONDS,
    )
    app.state.anon_device_limiter = SlidingWindowLimiter(
        max_attempts=_ANON_DEVICE_MAX_ATTEMPTS,
        window_seconds=_ANON_DEVICE_WINDOW_SECONDS,
    )
    app.state.anon_ip_limiter = SlidingWindowLimiter(
        max_attempts=_ANON_IP_MAX_ATTEMPTS,
        window_seconds=_ANON_IP_WINDOW_SECONDS,
    )
    try:
        app.state.credential_cipher = CredentialCipher.from_settings(settings)
    except CredentialCipherError:
        app.state.credential_cipher = None

    if settings.api_v3_base_path:
        _mount_api_v3(app, settings.api_v3_base_path)

    return app


def _mount_api_v3(app: FastAPI, prefix: str) -> None:
    """Mount the user-account (/v3) routers. Kept separate from _mount_api:
    the v2/v1 surface is device-centric and frozen; v3 is user-centric."""
    app.include_router(auth_routes.router, prefix=prefix)
    app.include_router(user_devices_routes.router, prefix=prefix)
    app.include_router(sync_routes.router, prefix=prefix)
    app.include_router(overrides_routes.router, prefix=f"{prefix}/sync")
    app.include_router(sync_jobs_routes.router, prefix=prefix)
    app.include_router(sync_jobs_routes.admin_router, prefix=prefix)
    app.include_router(academics_routes.courses_router, prefix=prefix)
    app.include_router(academics_routes.assignments_router, prefix=prefix)
    app.include_router(settings_docs_routes.router, prefix=prefix)
    app.include_router(academic_calendar_routes.router, prefix=prefix)
    app.include_router(bulletins_feed_routes.router, prefix=prefix)
    app.include_router(holiday_overrides_routes.router, prefix=f"{prefix}/sync")
    app.include_router(bulletins_v3_routes.subscriptions_router, prefix=prefix)
    app.include_router(bulletins_v3_routes.states_router, prefix=prefix)
    app.include_router(schedule_v3_routes.router, prefix=prefix)
    app.include_router(live_activities_v3_routes.router, prefix=prefix)




app = create_app()
