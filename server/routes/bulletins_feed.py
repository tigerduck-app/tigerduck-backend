"""Public bulletin feed (read-only) for the v3 app.

GET-only: taxonomy, paginated list, and detail. Open (no auth) — anyone
with the app should see public bulletins; the iOS/Android clients treat
`/bulletins`, `/bulletins/taxonomy`, `/bulletins/{id}` as public GETs.

Subscription rules and read/starred/hidden state live in the separate
`/bulletin-subscriptions` and `/bulletin-states` routers (JWT-scoped).

Restored at the `/v3` prefix after the v2 surface was sunset — the app's
base URL moved to /v3 but these read endpoints had not been ported.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, or_, select

from server.bulletins.models import Bulletin
from server.bulletins.schemas import (
    BulletinDetail,
    BulletinListResponse,
    BulletinSummary,
    OrgLabel,
    TagLabel,
    TaxonomyResponse,
)
from server.bulletins.taxonomy import (
    DEFAULT_TAGS_FOR_NEW_USER,
    ORG_LABELS,
    TAG_LABELS,
    CanonicalOrg,
    ContentTag,
)
from server.db import SessionDep

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/bulletins", tags=["bulletins"])


@router.get("/taxonomy", response_model=TaxonomyResponse)
async def get_taxonomy() -> TaxonomyResponse:
    """Return the full org + tag lookup so the client can render the
    subscription editor without hardcoding string IDs."""
    return TaxonomyResponse(
        orgs=[OrgLabel(id=org, label=ORG_LABELS[org]) for org in CanonicalOrg],
        tags=[TagLabel(id=tag, label=TAG_LABELS[tag]) for tag in ContentTag],
        default_tags=sorted(DEFAULT_TAGS_FOR_NEW_USER),
    )


def _coerce_org(raw: str | None) -> CanonicalOrg | None:
    """Tolerate DB rows that still carry a dropped enum value (e.g. during
    the re-classification window after a taxonomy change). Returns None
    for unknown values so the client sees an unclassified row instead of
    the endpoint erroring out."""
    if not raw:
        return None
    try:
        return CanonicalOrg(raw)
    except ValueError:
        return None


def _coerce_tags(raw: list[str] | None) -> list[ContentTag]:
    """Same pattern as `_coerce_org` but for the multi-valued tag array —
    silently drop entries that no longer map to a known enum."""
    result: list[ContentTag] = []
    for t in raw or []:
        try:
            result.append(ContentTag(t))
        except ValueError:
            continue
    return result


def _to_summary(row: Bulletin) -> BulletinSummary:
    return BulletinSummary(
        id=row.id,
        external_id=row.external_id,
        title=row.title,
        title_clean=row.title_clean,
        canonical_org=_coerce_org(row.canonical_org),
        content_tags=_coerce_tags(row.content_tags),
        importance=row.importance,  # type: ignore[arg-type]
        summary=row.summary,
        source_url=row.source_url,
        posted_at=row.posted_at,
        is_deleted=row.is_deleted,
        source=row.source,
    )


@router.get("", response_model=BulletinListResponse)
async def list_bulletins(
    session: SessionDep,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=0),
    include_deleted: bool = Query(default=False),
) -> BulletinListResponse:
    """Paginate processed bulletins, newest first by `posted_at`.

    Sort key is `(posted_at DESC, id DESC)`. The `cursor` is the id of the
    last item from the previous page, interpreted as "items strictly older
    than the cursor row" so a pinned post never appears on two pages.
    Deleted bulletins are hidden by default.
    """
    stmt = (
        select(Bulletin)
        .where(Bulletin.canonical_org.isnot(None))
        .order_by(Bulletin.posted_at.desc().nulls_last(), Bulletin.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        cursor_row = (
            await session.execute(
                select(Bulletin.posted_at, Bulletin.id).where(Bulletin.id == cursor)
            )
        ).first()
        if cursor_row is not None:
            # Decompose into nullable-safe predicates so NULL-posted rows
            # still stream through after the dated tail of page 1.
            if cursor_row.posted_at is not None:
                stmt = stmt.where(
                    or_(
                        Bulletin.posted_at < cursor_row.posted_at,
                        and_(
                            Bulletin.posted_at == cursor_row.posted_at,
                            Bulletin.id < cursor_row.id,
                        ),
                        Bulletin.posted_at.is_(None),
                    )
                )
            else:
                stmt = stmt.where(
                    Bulletin.posted_at.is_(None),
                    Bulletin.id < cursor_row.id,
                )
        # else: cursor row vanished between requests; serve from the start.
    if not include_deleted:
        stmt = stmt.where(Bulletin.is_deleted.is_(False))

    rows = (await session.execute(stmt)).scalars().all()
    has_next = len(rows) > limit
    items = [_to_summary(r) for r in rows[:limit]]
    next_cursor = items[-1].id if has_next and items else None
    return BulletinListResponse(items=items, next_cursor=next_cursor)


@router.get("/{bulletin_id}", response_model=BulletinDetail)
async def get_bulletin(bulletin_id: int, session: SessionDep) -> BulletinDetail:
    row = await session.get(Bulletin, bulletin_id)
    if row is None:
        raise HTTPException(status_code=404, detail="bulletin not found")
    base = _to_summary(row).model_dump()
    return BulletinDetail(
        **base,
        body_clean=row.body_clean,
        body_md=row.body_md,
        raw_publisher=row.raw_publisher,
    )
