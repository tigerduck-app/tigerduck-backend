"""In-memory foreground-poll tracker.

Devices poll ``GET /sync/revision`` every ~10 s while foregrounded.
The push pipeline skips delivery to devices that polled within the
threshold (they'll pick up changes via the next poll).

Single-process only — a multi-worker deploy would need Redis or a
shared-memory store.  For ≤4000 devices this is fine.
"""

from __future__ import annotations

import time
import threading

_lock = threading.Lock()
_polls: dict[str, float] = {}

_CLEANUP_THRESHOLD = 10_000
_STALE_SECONDS = 3600.0


def record_poll(user_id: str, device_id: str) -> None:
    key = f"{user_id}:{device_id}"
    now = time.monotonic()
    with _lock:
        _polls[key] = now
        if len(_polls) > _CLEANUP_THRESHOLD:
            _cleanup(now)


def is_foreground(user_id: str, device_id: str, threshold: float = 20.0) -> bool:
    key = f"{user_id}:{device_id}"
    with _lock:
        ts = _polls.get(key)
    if ts is None:
        return False
    return (time.monotonic() - ts) < threshold


def _cleanup(now: float) -> None:
    stale = [k for k, v in _polls.items() if now - v > _STALE_SECONDS]
    for k in stale:
        del _polls[k]
