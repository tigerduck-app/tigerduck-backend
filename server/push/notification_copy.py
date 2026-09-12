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
from typing import Any

from server.i18n import translate

REAUTH_SCENARIO = "reauth_required"

#: What an assignment reminder's payload carries so its copy can be written
#: per recipient. `push/reminders.py` writes these; the payload a device
#: receives has them replaced by the finished `title` and `body`.
REMINDER_COPY_INPUTS = ("assignment_title", "course_name")

_REMINDER_TITLE_KEY = "notification_assignment_reminder_title"

# The bundles use Android/Apple positional placeholders (`%1$s`), and `%%`
# for a literal percent sign.
_PLACEHOLDER = re.compile(r"%(\d+)\$s|%%")


def build_reauth_payload(*, provider: str, locale: str | None) -> dict:
    """Push payload telling the user their provider credential expired.

    `kind` is what both clients route on; without it the message reaches
    iOS as an untyped alert and is dropped outright by Android's FCM
    handler, which keys on the payload shape.
    """
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


def build_assignment_reminder_payload(
    payload: dict[str, Any], *, locale: str | None
) -> dict[str, Any]:
    """The assignment reminder one recipient receives, in `locale`.

    The same copy the apps used when they scheduled these reminders
    themselves: the title names the course, the body names the course and
    the assignment, and the body's wording follows the offset
    (`reminder_body_key`). No body states a fractional hour -- only `24h`
    names a number at all.

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

    # A title that ends in an empty course name says nothing, so it names
    # the assignment instead when the course is unknown.
    title = _render(
        translate(_REMINDER_TITLE_KEY, locale), course_name or assignment_title
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
