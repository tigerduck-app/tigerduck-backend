"""Phase 4c: user-level bulletin fan-out via push_jobs (spec §5).

Two passes per tick, both crash-idempotent:

  A. Match — bulletins that are LLM-processed but have no
     `bulletin_user_match_runs` row are matched against every enabled
     `user_bulletin_subscriptions` rule of every signed-in device (same
     Rule semantics as the anonymous matcher). Rules are per device, so a
     user's hit becomes one `bulletin_user_matches` row naming the devices
     whose own rules matched (ON CONFLICT DO NOTHING), and the run marker
     is written.
  B. Push — matches with `pushed_at IS NULL` get one push_job each
     (dedupe key `bulletin:{id}`; an existing active job is adopted
     instead of erroring), aimed at those devices alone, and are stamped
     with push_job_id/pushed_at.

The anonymous flow's `bulletins.notified_at` is never touched here —
that column remains the cursor of the device_registrations pipeline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import PushJob, UserDevice
from server.bulletins.matcher import Rule, rule_hits
from server.bulletins.models import Bulletin, BulletinProcessingState
from server.bulletins.taxonomy import SubscriptionMode
from server.config import Settings
from server.db import session_scope
from server.sync.models import (
    BulletinUserMatch,
    BulletinUserMatchRun,
    UserBulletinSubscription,
)

logger = structlog.get_logger(__name__)

CHANNEL = "bulletin"
SCENARIO = "bulletin_matched"


async def _match_new_bulletins(
    session: AsyncSession, settings: Settings, now: datetime
) -> int:
    """Pass A. Returns the number of bulletins examined."""
    bulletins = (
        (
            await session.execute(
                select(Bulletin)
                .where(
                    Bulletin.processing_state
                    == BulletinProcessingState.processed.value,
                    Bulletin.is_deleted.is_(False),
                    ~exists(
                        select(BulletinUserMatchRun.bulletin_id).where(
                            BulletinUserMatchRun.bulletin_id == Bulletin.id
                        )
                    ),
                )
                .order_by(Bulletin.id)
                .limit(settings.bulletin_user_dispatch_batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not bulletins:
        return 0

    subs = (
        (
            await session.execute(
                select(UserBulletinSubscription)
                .join(UserDevice, UserDevice.id == UserBulletinSubscription.device_id)
                .where(
                    UserBulletinSubscription.enabled.is_(True),
                    UserBulletinSubscription.deleted_at.is_(None),
                    # A signed-out device keeps its rules for when it signs
                    # back in, but receives nothing meanwhile.
                    UserDevice.deleted_at.is_(None),
                )
                .order_by(UserBulletinSubscription.id)
            )
        )
        .scalars()
        .all()
    )
    # user -> device -> that device's rules
    per_user: dict[uuid.UUID, dict[uuid.UUID, list[tuple[int, Rule]]]] = {}
    for sub in subs:
        rule = Rule(
            orgs=frozenset(sub.orgs or ()),
            tags=frozenset(sub.tags or ()),
            mode=SubscriptionMode(sub.mode),
        )
        per_user.setdefault(sub.user_id, {}).setdefault(sub.device_id, []).append(
            (sub.id, rule)
        )

    for bulletin in bulletins:
        values: list[dict] = []
        if bulletin.canonical_org is not None:
            for user_id, devices in per_user.items():
                # device -> the first of its rules that matched
                hits: dict[uuid.UUID, int] = {}
                for device_id, rules in devices.items():
                    hit_sub_id = next(
                        (
                            sub_id
                            for sub_id, rule in rules
                            if rule_hits(
                                rule,
                                canonical_org=bulletin.canonical_org,
                                content_tags=bulletin.content_tags or [],
                            )
                        ),
                        None,
                    )
                    if hit_sub_id is not None:
                        hits[device_id] = hit_sub_id
                if hits:
                    first_sub_id = min(hits.values())
                    values.append(
                        {
                            "bulletin_id": bulletin.id,
                            "user_id": user_id,
                            "subscription_id": first_sub_id,
                            # Pass B aims the push at exactly these devices.
                            "match_reason": {
                                "subscription_id": first_sub_id,
                                "device_ids": sorted(str(d) for d in hits),
                            },
                            "matched_at": now,
                        }
                    )
        if values:
            await session.execute(
                pg_insert(BulletinUserMatch)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=["bulletin_id", "user_id"]
                )
            )
        await session.execute(
            pg_insert(BulletinUserMatchRun)
            .values(bulletin_id=bulletin.id, matched_at=now)
            .on_conflict_do_nothing()
        )
    return len(bulletins)


async def _push_pending_matches(session: AsyncSession, now: datetime) -> int:
    """Pass B. Returns the number of push_jobs created."""
    rows = (
        await session.execute(
            select(BulletinUserMatch, Bulletin)
            .join(Bulletin, Bulletin.id == BulletinUserMatch.bulletin_id)
            .where(BulletinUserMatch.pushed_at.is_(None))
            .with_for_update(skip_locked=True, of=BulletinUserMatch)
        )
    ).all()
    created = 0
    for match, bulletin in rows:
        if bulletin.is_deleted:
            # Pulled from the board before we pushed — close the match
            # without notifying anyone.
            match.pushed_at = now
            continue
        title = bulletin.title_clean or bulletin.title
        body = bulletin.summary or title
        dedupe_key = f"bulletin:{bulletin.id}"
        payload = {
            "kind": "bulletin",
            "title": title,
            "body": body,
            "bulletin_id": bulletin.id,
            "source_url": bulletin.source_url,
            "canonical_org": bulletin.canonical_org or "",
        }
        device_ids = (match.match_reason or {}).get("device_ids")
        if device_ids is not None:
            # Only the devices whose own rules matched. A match written
            # before rules were per device names none, and its job goes to
            # every device as it always did.
            payload["target_device_ids"] = device_ids
        result = await session.execute(
            pg_insert(PushJob)
            .values(
                user_id=match.user_id,
                dedupe_key=dedupe_key,
                channel=CHANNEL,
                scenario=SCENARIO,
                fire_at=now,
                payload=payload,
            )
            .on_conflict_do_nothing()
            .returning(PushJob.id)
        )
        job_id = result.scalar_one_or_none()
        if job_id is None:
            # An active job with this dedupe key already exists (re-run
            # after a partial commit) — adopt it. Filter to the statuses
            # the dedupe index covers so an older cancelled/failed row
            # can't shadow the live one.
            job_id = (
                await session.execute(
                    select(PushJob.id)
                    .where(
                        PushJob.user_id == match.user_id,
                        PushJob.dedupe_key == dedupe_key,
                        PushJob.status.in_(
                            ["pending", "processing", "sent", "partial_failed"]
                        ),
                    )
                    .order_by(PushJob.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        else:
            created += 1
        match.push_job_id = job_id
        match.pushed_at = now
    return created


async def dispatch_user_bulletins(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """One tick. Returns the number of push_jobs created."""
    now = datetime.now(UTC)
    async with session_scope(session_factory) as session:
        examined = await _match_new_bulletins(session, settings, now)
        created = await _push_pending_matches(session, now)
    if examined or created:
        logger.info(
            "bulletins.user_dispatch.tick", examined=examined, created=created
        )
    return created
