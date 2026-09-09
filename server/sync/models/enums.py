"""Enumerations shared across the sync models."""

from __future__ import annotations
from enum import StrEnum


class CourseSource(StrEnum):
    ntust_portal = "ntust_portal"
    user_added = "user_added"
class EnrollmentStatus(StrEnum):
    enrolled = "enrolled"
    dropped = "dropped"
    completed = "completed"
class AssignmentLocalStatus(StrEnum):
    none = "none"
    locally_completed = "locally_completed"
    ignored = "ignored"
    archived = "archived"
class SubscriptionMode(StrEnum):
    and_ = "AND"
    or_ = "OR"
class ChangeOperation(StrEnum):
    upsert = "upsert"
    delete = "delete"
class ChangeEntityType(StrEnum):
    course = "course"
    course_override = "course_override"
    course_skipped_date = "course_skipped_date"
    assignment = "assignment"
    assignment_override = "assignment_override"
    settings_document = "settings_document"
    bulletin_subscription = "bulletin_subscription"
    bulletin_state = "bulletin_state"
    bulletin_match = "bulletin_match"
    #: "Notify me anyway on this holiday" — see
    #: `server.academic_calendar.models.UserHolidayOverride`. The holiday
    #: itself is school-wide operator data and never syncs per user; only the
    #: exception does.
    holiday_override = "holiday_override"
SETTINGS_NAMESPACES = (
    "home_layout",
    "appearance",
    "assignment_display",
    "notification",
    "browser",
    "language",
    "schedule_display",
    "watch",
    "wearos",
)
