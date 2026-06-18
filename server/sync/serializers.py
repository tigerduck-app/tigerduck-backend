"""Dict serializers for sync payloads (full sync + entity GET endpoints).

Plain dicts instead of pydantic response models: the full-sync snapshot
spans eight entity shapes and the client consumes them as documents; the
field selection here is the API contract.
"""

from __future__ import annotations

from datetime import datetime

from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserBulletinState,
    UserBulletinSubscription,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def course_to_dict(c: UserCourse) -> dict:
    return {
        "id": c.id,
        "semester": c.semester,
        "course_key": c.course_key,
        "course_no": c.course_no,
        "source": c.source,
        "course_name": c.course_name,
        "course_name_en": c.course_name_en,
        "instructors": list(c.instructors or []),
        "credits": float(c.credits) if c.credits is not None else None,
        "classroom": c.classroom,
        "enrolled_count": c.enrolled_count,
        "max_count": c.max_count,
        "moodle_id": c.moodle_id,
        "schedule_json": c.schedule_json,
        "classroom_map": c.classroom_map,
        "enrollment_status": c.enrollment_status,
        "fetched_at": _iso(c.fetched_at),
        "deleted_at": _iso(c.deleted_at),
        "updated_at": _iso(c.updated_at),
    }


def course_override_to_dict(o: UserCourseOverride) -> dict:
    return {
        "user_course_id": o.user_course_id,
        "custom_names": o.custom_names or {},
        "custom_name_updated_at": _iso(o.custom_name_updated_at),
        "color_hex": o.color_hex,
        "color_hex_updated_at": _iso(o.color_hex_updated_at),
        "is_hidden": o.is_hidden,
        "is_hidden_updated_at": _iso(o.is_hidden_updated_at),
    }


def skipped_date_to_dict(s: UserCourseSkippedDate) -> dict:
    return {
        "id": s.id,
        "user_course_id": s.user_course_id,
        "skipped_on": s.skipped_on.isoformat(),
        "reason": s.reason,
        "deleted_at": _iso(s.deleted_at),
    }


def assignment_to_dict(a: UserAssignment) -> dict:
    return {
        "id": a.id,
        "user_course_id": a.user_course_id,
        "moodle_course_id": a.moodle_course_id,
        "moodle_assignment_id": a.moodle_assignment_id,
        "course_no": a.course_no,
        "course_name": a.course_name,
        "title": a.title,
        "due_at": _iso(a.due_at),
        "cutoff_at": _iso(a.cutoff_at),
        "allow_from_at": _iso(a.allow_from_at),
        "moodle_url": a.moodle_url,
        "intro_html": a.intro_html,
        "provider_is_submitted": a.provider_is_submitted,
        "provider_submitted_at": _iso(a.provider_submitted_at),
        "provider_grading_status": a.provider_grading_status,
        "provider_grade": a.provider_grade,
        "deleted_at": _iso(a.deleted_at),
        "updated_at": _iso(a.updated_at),
    }


def assignment_override_to_dict(o: UserAssignmentOverride) -> dict:
    return {
        "user_assignment_id": o.user_assignment_id,
        "local_status": o.local_status,
        "local_status_updated_at": _iso(o.local_status_updated_at),
        "note": o.note,
        "note_updated_at": _iso(o.note_updated_at),
    }


def settings_document_to_dict(d: UserSettingsDocument) -> dict:
    return {
        "namespace": d.namespace,
        "schema_version": d.schema_version,
        "document": d.document,
        "revision": d.revision,
        "updated_at": _iso(d.updated_at),
    }


def subscription_to_dict(s: UserBulletinSubscription) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "orgs": list(s.orgs or []),
        "tags": list(s.tags or []),
        "mode": s.mode,
        "enabled": s.enabled,
        "revision": s.revision,
        "deleted_at": _iso(s.deleted_at),
    }


def bulletin_state_to_dict(s: UserBulletinState) -> dict:
    return {
        "bulletin_id": s.bulletin_id,
        "is_read": s.is_read,
        "read_updated_at": _iso(s.read_updated_at),
        "first_read_at": _iso(s.first_read_at),
        "is_starred": s.is_starred,
        "starred_updated_at": _iso(s.starred_updated_at),
        "is_hidden": s.is_hidden,
        "hidden_updated_at": _iso(s.hidden_updated_at),
    }
