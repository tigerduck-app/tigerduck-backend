"""Per-recipient copy for the pushes the server composes itself.

Spec §4.1: the backend owns the wording of every notification it writes, in
each device's own language. One job fans out to every device on the
account, and those devices can be set to different languages, so the title
and body are resolved per delivery -- in `push.pipeline._send_one`, from
the recipient device's `locale` -- never at enqueue time. A job's payload
carries only what is language-independent.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from server.i18n import format_units, translate

REAUTH_SCENARIO = "reauth_required"

#: The `reason` of a reauth job filed for a device whose TigerSync session is
#: gone (`push/session_reauth.py`). Any other reason, or none, is the Moodle
#: credential's, the portal's "operator_retry" among them.
SESSION_REAUTH_REASON = "session"

#: What an assignment reminder's payload carries so its copy can be written
#: per recipient. `push/reminders.py` writes these; the payload a device
#: receives has them replaced by the finished `title` and `body`.
REMINDER_COPY_INPUTS = ("assignment_title", "course_name")

_REMINDER_TITLE_KEY = "notification_assignment_reminder_title_time_left"

#: How late an assignment reminder may go out and still state its offset as
#: the time left. The pipeline sends a due reminder within its 5-second tick
#: and the pre-send check's 5 seconds; every path that holds one back (a
#: retry round, an unverified check, the countdown wait, a restart) adds a
#: minute or more. Within 30 seconds the offset is also the time left
#: rounded to the nearest minute.
REMINDER_ON_TIME = timedelta(seconds=30)

_TWO_DAYS_MINUTES = 48 * 60

# The title Android gives the class reminders it posts itself.
_CLASS_REMINDER_TITLE_KEY = "notification_class_preparing_title"

# The bundles use Android/Apple positional placeholders (`%1$s`), and `%%`
# for a literal percent sign.
_PLACEHOLDER = re.compile(r"%(\d+)\$s|%%")


def build_reauth_payload(
    *, provider: str, locale: str | None, reason: str | None = None
) -> dict:
    """Push payload telling the user their provider credential expired, or,
    for `SESSION_REAUTH_REASON`, that the device lost its TigerSync session.

    `kind` is what both clients route on; without it the message reaches
    iOS as an untyped alert and is dropped outright by Android's FCM
    handler, which keys on the payload shape.
    """
    if reason == SESSION_REAUTH_REASON:
        return {
            "kind": REAUTH_SCENARIO,
            "reason": reason,
            "title": translate("notification_reauth_session_title", locale),
            "body": translate("notification_reauth_session_body", locale),
        }
    return {
        "kind": REAUTH_SCENARIO,
        "provider": provider,
        "title": translate("notification_reauth_required_title", locale),
        "body": translate("notification_reauth_required_body", locale),
    }


def reminder_body_key(offset_minutes: int) -> str:
    """The bundle key for the body of a reminder `offset_minutes` before
    the due date.

    For the eleven offsets the apps offer, this is iOS 2.0.2's
    `AssignmentReminderOffset.notificationBody`, which Android copies:
    48 h, 24 h, 16 h, 30 min and 5 min each have a body of their own;
    8, 4, 2 and 1 h share `multi_hour`; and 10 min, which has no key of its
    own, shares `15m`.

    The backend also sees offsets no app offers -- its own default, a
    document edited by hand -- so the mapping must be total. Such an offset
    gets the body of the nearest offered offset below it, or the 5-minute
    body when there is none. One exception: the `24h` body states its
    number ("24 hours left!"), so it is used for exactly 24 hours, and an
    offset between 24 and 48 hours gets the neutral `48h` body ("is due
    in a while") instead.
    """
    if offset_minutes == 24 * 60:
        return "notification_assignment_reminder_body_24h"
    if offset_minutes > 24 * 60:
        return "notification_assignment_reminder_body_48h"
    if offset_minutes >= 16 * 60:
        return "notification_assignment_reminder_body_16h"
    if offset_minutes >= 60:
        return "notification_assignment_reminder_body_multi_hour"
    if offset_minutes >= 30:
        return "notification_assignment_reminder_body_30m"
    if offset_minutes >= 10:
        return "notification_assignment_reminder_body_15m"
    return "notification_assignment_reminder_body_5m"


def has_reminder_copy_inputs(payload: dict[str, Any]) -> bool:
    """Whether a reminder payload carries the inputs its copy is written
    from, rather than a title and body fixed when the job was filed."""
    return "assignment_title" in payload


def _render(template: str, *args: str) -> str:
    """Fill a bundle string's positional placeholders.

    One pass over the template, so a value that itself contains `%2$s` is
    inserted as written rather than expanded again.
    """

    def substitute(match: re.Match[str]) -> str:
        if match.group(1) is None:
            return "%"
        index = int(match.group(1)) - 1
        return args[index] if 0 <= index < len(args) else match.group(0)

    return _PLACEHOLDER.sub(substitute, template)


def _minutes_left(offset_minutes: int, due_epoch: Any, now: datetime) -> int:
    """The time left the title states, in whole minutes: the offset for a
    reminder sent within `REMINDER_ON_TIME` of its fire time, otherwise the
    time from `now` to the due time, rounded down."""
    if type(due_epoch) is not int:
        return offset_minutes
    seconds_left = due_epoch - now.timestamp()
    if seconds_left >= offset_minutes * 60 - REMINDER_ON_TIME.total_seconds():
        return offset_minutes
    return int(seconds_left // 60)


def _time_left_text(minutes: int, locale: str | None) -> str:
    """`minutes`, at least one, in minutes under an hour, hours through two
    days and days beyond, with the two largest units that are not zero."""
    minutes = max(minutes, 1)
    hours = minutes // 60
    days, hours = divmod(hours, 24) if minutes > _TWO_DAYS_MINUTES else (0, hours)
    units = (
        (days, "duration-day"),
        (hours, "duration-hour"),
        (minutes % 60, "duration-minute"),
    )
    return format_units([unit for unit in units if unit[0]][:2], locale)


def build_assignment_reminder_payload(
    payload: dict[str, Any], *, locale: str | None, now: datetime
) -> dict[str, Any]:
    """The assignment reminder one recipient receives at `now`, in `locale`.

    The title states the time left until the due time (`_minutes_left`).
    The body is the apps' own, from when they scheduled these reminders
    themselves: it names the course and the assignment, and its wording
    follows the offset (`reminder_body_key`). No body states a fractional
    hour -- only `24h` names a number at all.

    A job filed before copy moved to send time carries no copy inputs, only
    the Chinese title and body it was filed with, and goes out as filed.
    `scan_assignment_reminders` rewrites such jobs while they are still
    pending, so only one that was already due when the new code started
    can reach this branch.
    """
    if not has_reminder_copy_inputs(payload):
        return payload

    assignment_title = str(payload.get("assignment_title") or "")
    course_name = str(payload.get("course_name") or "")
    offset_minutes = round(float(payload.get("offset_hours") or 0) * 60)

    title = _render(
        translate(_REMINDER_TITLE_KEY, locale),
        _time_left_text(
            _minutes_left(offset_minutes, payload.get("due_epoch"), now), locale
        ),
    )
    body = _render(
        translate(reminder_body_key(offset_minutes), locale),
        course_name,
        assignment_title,
    )
    if not course_name:
        # The empty course name leaves a double space in templates such as
        # '24 hours left! %1$s "%2$s"'.
        body = " ".join(body.split())

    delivered = {
        key: value
        for key, value in payload.items()
        if key not in REMINDER_COPY_INPUTS
    }
    delivered["title"] = title
    delivered["body"] = body
    return delivered


def build_course_reminder_payload(
    payload: dict[str, Any], *, locale: str | None
) -> dict[str, Any]:
    """The class reminder one recipient receives, titled in `locale`.

    The body, the class's start time and room, reads the same in every
    language and is written when the scan files the job. A job filed before
    the title moved to send time carries a Chinese title and goes out as
    filed; the scan files reminders at most 48 hours ahead.
    """
    if "course_name" not in payload:
        return payload
    delivered = {key: value for key, value in payload.items() if key != "course_name"}
    delivered["title"] = _render(
        translate(_CLASS_REMINDER_TITLE_KEY, locale), str(payload["course_name"] or "")
    )
    return delivered
