"""Which client versions still schedule their own assignment reminders.

The backend started sending assignment reminders in 2.1.0, the same
release that removed the iOS local scheduler. Between the backend
deploying and a given phone updating -- which may be weeks, or never on
F-Droid -- a 2.0.2 device would receive every reminder twice: once from
its own UNUserNotificationCenter, once from us.

So delivery is gated on the reported app version, and the gate fails
CLOSED. The two errors are not symmetric: skipping a device that turned
out to be new costs one reminder the user still gets locally, while
sending to a device that turned out to be old costs a duplicate
notification the user will read as a bug.
"""

from __future__ import annotations

import re

#: The release that removed the iOS local scheduler and started relying
#: on backend delivery. Keep in step with `MARKETING_VERSION` in
#: tigerduck-app and `versionName` in tigerduck-app-android.
BACKEND_REMINDERS_MIN_VERSION: tuple[int, ...] = (2, 1, 0)

#: Leading dotted integers only. Anything after them -- the F-Droid
#: flavor's "-fdroid", a "+build" tag -- is ignored, and anything that
#: does not start with a digit does not parse at all.
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)")

#: The only platforms the backend delivers assignment reminders to.
#: Everything else either schedules them locally (Android, Wear OS; spec
#: 4.2) or takes no notifications at all (macOS). Listing what IS allowed
#: rather than what is not makes a platform added later fail closed.
_BACKEND_REMINDER_PLATFORMS = frozenset({"ios", "ipados"})


def parse_app_version(raw: str | None) -> tuple[int, ...] | None:
    if not raw:
        return None
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def schedules_reminders_locally(platform: str, app_version: str | None) -> bool:
    """True when the backend must NOT send this device an assignment
    reminder: it schedules its own (Android, Wear OS, iOS before 2.1.0),
    takes no notifications (macOS), or cannot be shown to be a 2.1.0+
    iPhone or iPad."""
    if platform not in _BACKEND_REMINDER_PLATFORMS:
        return True
    parsed = parse_app_version(app_version)
    if parsed is None:
        return True  # fail closed -- see the module docstring
    return parsed < BACKEND_REMINDERS_MIN_VERSION
