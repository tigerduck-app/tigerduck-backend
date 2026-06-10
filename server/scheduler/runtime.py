"""AsyncIOScheduler lifecycle tied to FastAPI's lifespan.

One scheduler per app instance. `max_instances=1` and `coalesce=True` on
the tick job prevent overlapping runs when a tick occasionally takes
longer than the interval — better to skip than double-send.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.bulletins import jobs as bulletin_jobs
from server.bulletins.llm.base import LLMProvider
from server.bulletins.user_dispatch import dispatch_user_bulletins
from server.bulletins.llm.openai_compat import OpenAICompatibleProvider
from server.config import Settings
from server.push.custom_push_dispatcher import dispatch_pending_custom_pushes
from server.push.router import PushRouter
from server.scheduler.dispatcher import dispatch_due_pushes
from server.scheduler.retention import prune_terminal_activity_tokens
from server.push.course_reminders import scan_course_reminders
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import scan_assignment_reminders
from server.sync.retention import purge_expired_changelog
from server.syncjobs.executor import SyncWorker, run_sync_tick


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
) -> AsyncIOScheduler:
    """Wire APScheduler with the TigerDuck jobs:

    * `dispatcher_tick` — existing PTS dispatcher (Live Activity).
    * `bulletin_scrape` — fetch NTUST bulletin list every 10 min.
    * `bulletin_process` — drain pending bulletins through the LLM every 60s.
    * `bulletin_dispatch` — fan out alert pushes every 60s.
    * `bulletin_retention` — prune aged-out soft-deleted bulletins daily.
    * `live_activity_token_retention` — prune terminal update-token rows daily.
    * `sync_changelog_retention` — purge aged changelog entries daily.
    * `sync_jobs_tick` — server-side academic sync executor every 30s
      (only when a `sync_worker` is provided, i.e. credential keys exist).
    * `push_pipeline_tick` — user push_jobs delivery pipeline every 30s
      (only when a `push_worker` is provided).

    Passing `llm=None` (the default) builds the real OpenAI-compatible
    provider; tests inject `RecordingProvider` to stay offline.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    llm_provider = llm if llm is not None else build_llm_provider(settings)

    async def pts_tick() -> None:
        # Live Activity / scheduled iOS pushes only need APNs.
        await dispatch_due_pushes(session_factory, router.apple, settings)

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

    async def live_activity_token_retention() -> None:
        await prune_terminal_activity_tokens(session_factory, settings)

    async def sync_changelog_retention() -> None:
        await purge_expired_changelog(session_factory, settings)

    scheduler.add_job(
        pts_tick,
        trigger=IntervalTrigger(seconds=settings.scheduler_tick_seconds),
        id="dispatcher_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
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
        live_activity_token_retention,
        trigger=IntervalTrigger(
            hours=settings.live_activity_token_retention_interval_hours
        ),
        id="live_activity_token_retention",
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
    return scheduler
