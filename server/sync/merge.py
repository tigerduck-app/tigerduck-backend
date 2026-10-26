"""Per-field merge for cross-device sync.

Entities like course/assignment overrides and bulletin states carry field
triplets: `<field>`, `<field>_updated_at`, `<field>_device_id`. A client
edit wins iff its (clamped) timestamp is strictly newer than the stored
one — so two devices editing different fields never clobber each other.

TigerSync edits carry a clock value: integer microseconds since the Unix
epoch (`to_clock` / `from_clock`), which a timestamp column holds exactly.
An edit sent without one is stamped with the time it arrives (`edit_time`).
A TigerSync stamp later than `now + MAX_CLOCK_SKEW` is refused with 409
`clock_ahead`: clamping it would stamp each retry of one edit later, so a
retry could beat an edit made meanwhile. Past stamps are kept, so offline
edits merge correctly. The older paths' timestamps are still clamped
(`apply_field`). Ties lose: server state wins on equal timestamps, keeping replays
idempotent. An edit carrying the stored value raises the stored stamp
instead of changing the value, so a later edit of the same value still
outranks an earlier, different one (`apply_field`).

Callers hold the user's sync lock (`lock_sync_state`) from the read of a
stored stamp to the write, so concurrent requests apply one after another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

MAX_CLOCK_SKEW = timedelta(seconds=5)
CLOCK_HEADER = "X-TigerSync-Clock"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TICK = timedelta(microseconds=1)
#: What a value sent without a time counts as: older than any stamp.
OLDEST = _EPOCH


def to_clock(ts: datetime) -> int:
    return (ts - _EPOCH) // _TICK


def from_clock(value: int) -> datetime:
    return _EPOCH + value * _TICK


def clamp_ts(
    client_ts: datetime, now: datetime, *, skew: timedelta = timedelta(0)
) -> datetime:
    return min(client_ts, now + skew)


def edit_time(clock: int | None, now: datetime) -> datetime:
    """A request's stamp as a time: arrival when it sent none. A stamp later
    than `now + MAX_CLOCK_SKEW` is refused before anything is written, and
    compared as an integer, so one past year 9999 never builds a datetime.
    """
    if clock is None:
        return now
    if clock > to_clock(now + MAX_CLOCK_SKEW):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="clock_ahead")
    return from_clock(clock)


def _speaks_clock_protocol(
    x_tigersync_protocol: Annotated[str | None, Header()] = None,
) -> bool:
    return x_tigersync_protocol == "2"


#: True for a request from an app that stamps its edits (`X-TigerSync-Protocol: 2`).
ClockProtocolDep = Annotated[bool, Depends(_speaks_clock_protocol)]


def require_stamp(speaks_clock: bool, clock: int | None) -> None:
    """An app that stamps its edits must stamp each one."""
    if speaks_clock and clock is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="hlc_required"
        )


def apply_field(
    entity: object,
    field: str,
    value: object,
    *,
    client_ts: datetime,
    device_id: uuid.UUID | None,
    now: datetime,
    stamp_field: str | None = None,
    device_field: str | None = None,
) -> bool:
    """Apply one field edit if it wins the per-field timestamp comparison.

    Returns True when the value changed. An edit with the stored value and a
    later stamp moves only the stamp and returns False. Expects `entity` to
    have `<field>`, `<field>_updated_at`, `<field>_device_id` attributes,
    unless `stamp_field` / `device_field` name others.
    """
    stamp_field = stamp_field or f"{field}_updated_at"
    device_field = device_field or f"{field}_device_id"
    effective_ts = clamp_ts(client_ts, now, skew=MAX_CLOCK_SKEW)
    stored_ts: datetime | None = getattr(entity, stamp_field)
    if stored_ts is not None and effective_ts <= stored_ts:
        return False
    changed = getattr(entity, field) != value
    if changed:
        setattr(entity, field, value)
    setattr(entity, stamp_field, effective_ts)
    if hasattr(entity, device_field):
        setattr(entity, device_field, device_id)
    return changed


def apply_course_name(
    override: object,
    locale: str,
    name: str,
    *,
    client_ts: datetime,
    device_id: uuid.UUID | None,
    now: datetime,
) -> bool:
    """`apply_field` for one locale of `custom_names`, stamped per locale in
    `custom_names_updated_at` (clock values). `custom_name_updated_at` keeps
    the latest stamp of any locale for readers that predate the map."""
    effective_ts = clamp_ts(client_ts, now, skew=MAX_CLOCK_SKEW)
    stamps = dict(override.custom_names_updated_at or {})
    stored = stamps.get(locale)
    if stored is not None and to_clock(effective_ts) <= stored:
        return False
    names = dict(override.custom_names or {})
    changed = names.get(locale) != name
    if changed:
        names[locale] = name
        override.custom_names = names
    stamps[locale] = to_clock(effective_ts)
    override.custom_names_updated_at = stamps
    previous = override.custom_name_updated_at
    if previous is None or effective_ts > previous:
        override.custom_name_updated_at = effective_ts
    override.custom_name_device_id = device_id
    return changed


def set_locale_less_name(
    override: object,
    name: str | None,
    *,
    client_ts: datetime,
    device_id: uuid.UUID | None,
    now: datetime,
) -> bool:
    """A name sent without a locale, as the academics PUT and the initial
    upload send one, is the name in both locales the apps read."""
    changed = False
    for locale in ("zh", "en"):
        changed |= apply_course_name(
            override, locale, name or "", client_ts=client_ts, device_id=device_id, now=now
        )
    return changed
