"""Per-field merge for cross-device sync.

Entities like course/assignment overrides and bulletin states carry field
triplets: `<field>`, `<field>_updated_at`, `<field>_device_id`. A client
edit wins iff its (clamped) timestamp is strictly newer than the stored
one — so two devices editing different fields never clobber each other.

Clock-skew rule: client timestamps are clamped to
`min(client_ts, server_now)`. Past timestamps are kept (offline edits
merge correctly); future timestamps are pulled back to now, so a device
with a broken clock can't write a value that permanently outranks every
honest edit for the next year. Ties lose: server state wins on equal
timestamps, keeping replays idempotent.

Idempotency caveat: replays are only exact for non-future client
timestamps. A future timestamp clamps to arrival time, so a retried
request clamps to a slightly later instant and re-applies (bumping the
field's merge metadata, not its value). Harmless, but worth knowing when
reading changelog traffic from clock-skewed devices.
"""

from __future__ import annotations

import uuid
from datetime import datetime


def clamp_ts(client_ts: datetime, now: datetime) -> datetime:
    return min(client_ts, now)


def apply_field(
    entity: object,
    field: str,
    value: object,
    *,
    client_ts: datetime,
    device_id: uuid.UUID | None,
    now: datetime,
) -> bool:
    """Apply one field edit if it wins the per-field timestamp comparison.

    Returns True when the entity was modified. Expects `entity` to have
    `<field>`, `<field>_updated_at`, `<field>_device_id` attributes.
    """
    effective_ts = clamp_ts(client_ts, now)
    stored_ts: datetime | None = getattr(entity, f"{field}_updated_at")
    if stored_ts is not None and effective_ts <= stored_ts:
        return False
    setattr(entity, field, value)
    setattr(entity, f"{field}_updated_at", effective_ts)
    setattr(entity, f"{field}_device_id", device_id)
    return True
