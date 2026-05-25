"""Shared device-filter resolution for the custom-push pipeline.

Used by:
* `POST /api/custom-push/preview` — operator dry-run count.
* `POST /api/custom-push` — actual send (record-keeping path stamps the
  filter into `Bulletin.dispatch_filter_json`; pure-notification path
  resolves and fans out into `custom_push_dispatches`).
* The bulletin dispatcher when `bulletin.dispatch_filter_json` is set.
* The custom-push dispatcher.

Keeping this in one place guarantees the preview count and the actual
send target the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import DeviceListMember, DevicePlatform, DeviceRegistration

TargetClass = Literal["iphone", "ipad", "android"]


@dataclass(frozen=True)
class TargetFilter:
    target_classes: list[TargetClass] = field(default_factory=list)
    user_id: str | None = None
    device_id: str | None = None
    # Operator-managed cohort (see `device_lists` table). When set, ANDs
    # with the other filters — e.g. "iPhone members of list 7", not "list
    # 7 OR iPhones". Empty list (`list_id` set but no members yet)
    # naturally yields zero matches.
    list_id: int | None = None


def _legacy_platforms_for(classes: list[str]) -> set[str]:
    """Legacy rows (device_class='') predate the column; fall back to
    matching by platform."""
    out: set[str] = set()
    if "iphone" in classes or "ipad" in classes or "mac" in classes:
        out.add("apple")
    if "android" in classes:
        out.add("android")
    return out


async def resolve_target_device_ids(
    session: AsyncSession,
    filt: TargetFilter,
) -> list[str]:
    """Return device_ids matching the filter. Order: ascending device_id."""
    classes = list(filt.target_classes)
    if not classes:
        return []

    legacy_platforms = _legacy_platforms_for(classes)
    class_clause = DeviceRegistration.device_class.in_(classes)
    legacy_clause = (
        (DeviceRegistration.device_class == "")
        & (DeviceRegistration.platform.in_(legacy_platforms))
        if legacy_platforms
        else None
    )

    # Apple alert pushes consume the standard APNs `device_token_hex`;
    # Android pushes go via FCM (token stored in `pts_token_hex`). Mirrors
    # the gate in `server/bulletins/matcher.py` — keep them in sync.
    token_clause = or_(
        and_(
            DeviceRegistration.platform == DevicePlatform.apple.value,
            DeviceRegistration.device_token_hex.isnot(None),
            DeviceRegistration.device_token_hex != "",
        ),
        and_(
            DeviceRegistration.platform == DevicePlatform.android.value,
            DeviceRegistration.pts_token_hex.isnot(None),
            DeviceRegistration.pts_token_hex != "",
        ),
    )
    stmt = select(DeviceRegistration.device_id).where(
        DeviceRegistration.server_push_enabled.is_(True),
        token_clause,
        or_(class_clause, legacy_clause) if legacy_clause is not None else class_clause,
    )
    if filt.user_id is not None:
        stmt = stmt.where(DeviceRegistration.user_id == filt.user_id)
    if filt.device_id is not None:
        stmt = stmt.where(DeviceRegistration.device_id == filt.device_id)
    if filt.list_id is not None:
        stmt = stmt.where(
            DeviceRegistration.device_id.in_(
                select(DeviceListMember.device_id).where(
                    DeviceListMember.list_id == filt.list_id
                )
            )
        )
    stmt = stmt.order_by(DeviceRegistration.device_id)

    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def count_by_class(
    session: AsyncSession,
    filt: TargetFilter,
) -> dict[str, int]:
    """Per-class breakdown for the preview endpoint."""
    ids = await resolve_target_device_ids(session, filt)
    counts: dict[str, int] = {cls: 0 for cls in filt.target_classes}
    if not ids:
        return {**counts, "total": 0}

    rows = (
        await session.execute(
            select(
                DeviceRegistration.device_class,
                DeviceRegistration.platform,
            ).where(DeviceRegistration.device_id.in_(ids))
        )
    ).all()
    for device_class, platform in rows:
        if device_class in counts:
            counts[device_class] += 1
        elif device_class == "" and "iphone" in counts and platform == "apple":
            counts["iphone"] += 1
        elif (
            device_class == ""
            and "ipad" in counts
            and "iphone" not in counts
            and platform == "apple"
        ):
            counts["ipad"] += 1
        elif device_class == "" and "android" in counts and platform == "android":
            counts["android"] += 1
    return {**counts, "total": sum(counts.values())}
