"""AsyncIOScheduler lifecycle tied to FastAPI's lifespan.

One scheduler per app instance. `max_instances=1` and `coalesce=True` on
the tick job prevent overlapping runs when a tick occasionally takes
longer than the interval — better to skip than double-send.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.admin_audit import prune_admin_audit_log
from server.auth.retirement import retire_dead_devices
from server.bulletins import jobs as bulletin_jobs
from server.bulletins.llm.base import LLMProvider
from server.bulletins.user_dispatch import dispatch_user_bulletins
from server.bulletins.llm.openai_compat import OpenAICompatibleProvider
from server.config import Settings
from server.course_catalog.refresh import refresh_course_catalog
from server.links.checker import run_link_checks
from server.metrics import instrument_scheduler
from server.push.custom_push_dispatcher import dispatch_pending_custom_pushes
from server.push.router import PushRouter
from server.push.course_reminders import scan_course_reminders
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.retention import prune_terminal_push_jobs
from server.push.reminders import scan_assignment_reminders
from server.push.session_reauth import scan_sessionless_devices
from server.sync.retention import purge_expired_changelog
from server.syncjobs.executor import SyncWorker, run_sync_tick
from server.db import session_scope
from server.tls_pins.document import PinSigner, ensure_current_document
from server.tls_pins.watcher import run_pin_watch


def build_llm_provider(settings: Settings) -> LLMProvider:
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
    )


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    router: PushRouter,
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    sync_worker: SyncWorker | None = None,
    push_worker: PushPipelineWorker | None = None,
    tls_pin_signer: PinSigner | None = None,
) -> AsyncIOScheduler:
    """Wire APScheduler with the TigerDuck jobs:

    * `bulletin_scrape` — fetch NTUST bulletin list every 10 min.
    * `bulletin_process` — drain pending bulletins through the LLM every 60s.
    * `bulletin_dispatch` — fan out alert pushes every 60s.
    * `bulletin_retention` — prune aged-out soft-deleted bulletins daily.
    * `sync_changelog_retention` — purge aged changelog entries daily.
    * `push_job_retention` — prune terminal push_jobs (+ cascade deliveries) after 7 days.
    * `admin_audit_retention` — prune portal audit rows after 365 days, daily.
    * `device_retirement` — deregister the devices that are gone, daily at
      04:00 Taipei.
    * `sync_jobs_tick` — server-side academic sync executor every 30s
      (only when a `sync_worker` is provided, i.e. credential keys exist).
    * `push_pipeline_tick` — user push_jobs delivery pipeline every 5s
      (only when a `push_worker` is provided).
    * `session_reauth_scan` — ask devices left without a session to sign in
      again every 10 min (only with a `push_worker`, which delivers it, and
      not at all with its interval set to 0).
    * `tls_pin_watch` — check every monitored host's TLS chain hourly,
      first a minute after start (not at all with its interval set to 0).
    * `tls_pin_resign` — sign the pin document again when 7 days are left,
      checked hourly whatever the watcher's interval (only with a key).
    * `link_check` — export each link's alert state and check the links due a
      check, every minute and once at startup (not at all with its interval
      set to 0).
    * `course_catalog_refresh` — fetch the course catalog from NTUST every
      `course_catalog_refresh_interval_minutes`, first one interval after
      start (not at all with it set to 0).

    Passing `llm=None` (the default) builds the real OpenAI-compatible
    provider; tests inject `RecordingProvider` to stay offline.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    llm_provider = llm if llm is not None else build_llm_provider(settings)

    async def bulletin_scrape() -> None:
        await bulletin_jobs.scrape_job(session_factory, settings)

    async def bulletin_process() -> None:
        await bulletin_jobs.process_job(session_factory, settings, llm_provider)

    async def bulletin_dispatch() -> None:
        await bulletin_jobs.dispatch_job(session_factory, router, settings)

    async def custom_push_dispatch() -> None:
        await dispatch_pending_custom_pushes(session_factory, router, settings)

    async def bulletin_retention() -> None:
        await bulletin_jobs.retention_job(session_factory, settings)

    async def sync_changelog_retention() -> None:
        await purge_expired_changelog(session_factory, settings)

    async def push_job_retention() -> None:
        await prune_terminal_push_jobs(session_factory, settings)

    async def admin_audit_retention() -> None:
        await prune_admin_audit_log(session_factory, settings)

    async def device_retirement() -> None:
        await retire_dead_devices(session_factory)

    scheduler.add_job(
        bulletin_scrape,
        trigger=IntervalTrigger(seconds=settings.bulletin_scrape_interval_seconds),
        id="bulletin_scrape",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        bulletin_process,
        trigger=IntervalTrigger(seconds=settings.bulletin_process_interval_seconds),
        id="bulletin_process",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        bulletin_dispatch,
        trigger=IntervalTrigger(seconds=settings.bulletin_dispatch_interval_seconds),
        id="bulletin_dispatch",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        custom_push_dispatch,
        trigger=IntervalTrigger(seconds=settings.bulletin_dispatch_interval_seconds),
        id="custom_push_dispatch",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        bulletin_retention,
        trigger=IntervalTrigger(hours=settings.bulletin_retention_interval_hours),
        id="bulletin_retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        sync_changelog_retention,
        trigger=IntervalTrigger(
            hours=settings.sync_changelog_retention_interval_hours
        ),
        id="sync_changelog_retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        push_job_retention,
        trigger=IntervalTrigger(
            hours=settings.push_job_retention_interval_hours
        ),
        id="push_job_retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        admin_audit_retention,
        trigger=IntervalTrigger(hours=24),
        id="admin_audit_retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        device_retirement,
        # A wall-clock time, so a deploy cannot keep putting the run off a day;
        # before the host's 04:30 backup (deploy/launchd/ai.tigerduck.backup.plist).
        trigger=CronTrigger(hour=4, timezone="Asia/Taipei"),
        id="device_retirement",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    if sync_worker is not None:

        async def sync_jobs_tick() -> None:
            await run_sync_tick(sync_worker)

        scheduler.add_job(
            sync_jobs_tick,
            trigger=IntervalTrigger(seconds=settings.sync_job_tick_seconds),
            id="sync_jobs_tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
    if push_worker is not None:

        async def push_pipeline_tick() -> None:
            await run_push_tick(push_worker)

        async def assignment_reminder_scan() -> None:
            await scan_assignment_reminders(session_factory, settings)

        async def course_reminder_scan() -> None:
            await scan_course_reminders(session_factory, settings)

        async def bulletin_user_dispatch() -> None:
            await dispatch_user_bulletins(session_factory, settings)

        async def session_reauth_scan() -> None:
            await scan_sessionless_devices(session_factory, settings)

        scheduler.add_job(
            push_pipeline_tick,
            trigger=IntervalTrigger(seconds=settings.push_pipeline_tick_seconds),
            id="push_pipeline_tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        scheduler.add_job(
            assignment_reminder_scan,
            trigger=IntervalTrigger(
                seconds=settings.assignment_reminder_scan_interval_seconds
            ),
            id="assignment_reminder_scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            course_reminder_scan,
            trigger=IntervalTrigger(
                seconds=settings.course_reminder_scan_interval_seconds
            ),
            id="course_reminder_scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            bulletin_user_dispatch,
            trigger=IntervalTrigger(
                seconds=settings.bulletin_user_dispatch_interval_seconds
            ),
            id="bulletin_user_dispatch",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        if settings.session_reauth_scan_interval_seconds > 0:
            scheduler.add_job(
                session_reauth_scan,
                trigger=IntervalTrigger(
                    seconds=settings.session_reauth_scan_interval_seconds
                ),
                id="session_reauth_scan",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )
    if settings.tls_pin_watch_interval_seconds > 0:

        async def tls_pin_watch() -> None:
            await run_pin_watch(
                session_factory,
                tls_pin_signer,
                timeout=settings.tls_pin_watch_timeout_seconds,
            )

        scheduler.add_job(
            tls_pin_watch,
            trigger=IntervalTrigger(seconds=settings.tls_pin_watch_interval_seconds),
            id="tls_pin_watch",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            # A deploy would otherwise leave the chain gauges empty for an hour.
            next_run_time=datetime.now(UTC) + timedelta(minutes=1),
        )
    if tls_pin_signer is not None:

        async def tls_pin_resign() -> None:
            async with session_scope(session_factory) as session:
                await ensure_current_document(session, tls_pin_signer)

        scheduler.add_job(
            tls_pin_resign,
            trigger=IntervalTrigger(hours=1),
            id="tls_pin_resign",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    if settings.link_check_tick_seconds > 0:

        async def link_check() -> None:
            await run_link_checks(session_factory, settings)

        scheduler.add_job(
            link_check,
            trigger=IntervalTrigger(seconds=settings.link_check_tick_seconds),
            id="link_check",
            # At startup, so the exported alert state survives a restart.
            next_run_time=datetime.now(UTC),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
    if settings.course_catalog_refresh_interval_minutes > 0:

        async def course_catalog_refresh() -> None:
            await refresh_course_catalog(session_factory)

        scheduler.add_job(
            course_catalog_refresh,
            trigger=IntervalTrigger(minutes=settings.course_catalog_refresh_interval_minutes),
            id="course_catalog_refresh",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    # After every add_job, so each job's series start at zero.
    instrument_scheduler(scheduler)
    return scheduler
