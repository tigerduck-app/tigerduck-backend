"""Sync-domain ORM models.

Split by subject rather than kept in one 762-line module. Every name the
old `server.sync.models` exposed is re-exported here, so existing imports
resolve unchanged — and importing this package still registers every
table on `Base.metadata`, which is what Alembic autogenerate depends on.
"""
from .enums import (
    AssignmentLocalStatus,
    ChangeEntityType,
    ChangeOperation,
    CourseSource,
    EnrollmentStatus,
    SETTINGS_NAMESPACES,
    SubscriptionMode,
)
from .courses import (
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserCourseTombstone,
)
from .assignments import (
    UserAssignment,
    UserAssignmentOverride,
)
from .settings import (
    UserSettingsDocument,
)
from .bulletins import (
    BulletinUserMatch,
    BulletinUserMatchRun,
    UserBulletinState,
    UserBulletinSubscription,
)
from .state import (
    UserChangeLog,
    UserSyncState,
)

__all__ = [
    "AssignmentLocalStatus",
    "BulletinUserMatch",
    "BulletinUserMatchRun",
    "ChangeEntityType",
    "ChangeOperation",
    "CourseSource",
    "EnrollmentStatus",
    "SETTINGS_NAMESPACES",
    "SubscriptionMode",
    "UserAssignment",
    "UserAssignmentOverride",
    "UserBulletinState",
    "UserBulletinSubscription",
    "UserChangeLog",
    "UserCourse",
    "UserCourseOverride",
    "UserCourseSkippedDate",
    "UserCourseTombstone",
    "UserSettingsDocument",
    "UserSyncState",
]
