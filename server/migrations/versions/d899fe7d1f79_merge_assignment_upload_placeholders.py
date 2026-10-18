"""Merge the apps' assignment placeholders into the server's rows

Revision ID: d899fe7d1f79
Revises: df3948adb76a
Create Date: 2026-10-18 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd899fe7d1f79'
down_revision: str | Sequence[str] | None = 'df3948adb76a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Each placeholder (course id 0) that has a server row of its assignment, the
# server row chosen as the override PATCH chooses it. `keep` marks one whose
# mark `/sync/full` no longer keys (`_hidden_since_received` in
# server/routes/sync/snapshot.py) while the server row's override, if any,
# settles nothing: it holds no mark, and no clear as late as that mark. The
# apps dropped such a mark long ago, and it may be one the user cleared under
# 3.2.0, which left the placeholder's mark behind, so the pair stays as it is.
_PAIRS = """
CREATE TEMP TABLE placeholder_merges ON COMMIT DROP AS
SELECT
    p.id AS placeholder_id,
    s.id AS server_id,
    (
        p.deleted_at IS NOT NULL
        AND po.local_status <> 'none'
        AND NOT EXISTS (
            SELECT 1 FROM user_assignment_overrides so
            WHERE so.user_assignment_id = s.id
                AND (
                    so.local_status <> 'none'
                    OR COALESCE(so.local_status_updated_at, '-infinity')
                        >= COALESCE(po.local_status_updated_at, '-infinity')
                )
        )
        AND (
            EXISTS (
                SELECT 1 FROM user_assignments live
                WHERE live.user_id = p.user_id
                    AND live.moodle_assignment_id = p.moodle_assignment_id
                    AND live.moodle_course_id <> 0
                    AND live.deleted_at IS NULL
            )
            OR (
                SELECT max(gone.deleted_at) FROM user_assignments gone
                WHERE gone.user_id = p.user_id
                    AND gone.moodle_assignment_id = p.moodle_assignment_id
                    AND gone.moodle_course_id <> 0
            ) > GREATEST(
                p.deleted_at,
                COALESCE(po.local_status_received_at, po.updated_at)
            )
        )
    ) IS TRUE AS keep
FROM user_assignments p
JOIN LATERAL (
    SELECT s.id FROM user_assignments s
    WHERE s.user_id = p.user_id
        AND s.moodle_assignment_id = p.moodle_assignment_id
        AND s.moodle_course_id <> 0
    ORDER BY s.deleted_at IS NOT NULL, s.id
    LIMIT 1
) s ON true
LEFT JOIN user_assignment_overrides po ON po.user_assignment_id = p.id
WHERE p.moodle_course_id = 0
"""

# What the submission and grade folds, the merges and the move change, for
# the change log: the user, the server row, and whether an override (a mark
# or a note) did.
_CHANGES = """
CREATE TEMP TABLE placeholder_merge_changes (
    user_id uuid NOT NULL,
    server_id bigint NOT NULL,
    override boolean NOT NULL
) ON COMMIT DROP
"""

# Moodle never takes a submission back, so either row's counts.
_FOLD_SUBMISSION = """
WITH changed AS (
    UPDATE user_assignments s
    SET provider_is_submitted = true,
        provider_submitted_at = COALESCE(
            s.provider_submitted_at, p.provider_submitted_at
        ),
        updated_at = now()
    FROM placeholder_merges m
    JOIN user_assignments p ON p.id = m.placeholder_id
    WHERE s.id = m.server_id
        AND p.provider_is_submitted
        AND NOT s.provider_is_submitted
    RETURNING s.user_id, s.id
)
INSERT INTO placeholder_merge_changes SELECT user_id, id, false FROM changed
"""

# No fetch writes a grade, so an app's upload is the only source: the
# placeholder's fills a server row that has none.
_FOLD_GRADE = """
WITH changed AS (
    UPDATE user_assignments s
    SET provider_grade = p.provider_grade,
        updated_at = now()
    FROM placeholder_merges m
    JOIN user_assignments p ON p.id = m.placeholder_id
    WHERE s.id = m.server_id
        AND p.provider_grade IS NOT NULL
        AND s.provider_grade IS NULL
    RETURNING s.user_id, s.id
)
INSERT INTO placeholder_merge_changes SELECT user_id, id, false FROM changed
"""

# Where both rows have an override, each field keeps the later value, as the
# per-field merge does (server/sync/merge.py): the mark by its time and the
# note by its own, the server row's winning a tie.
_MERGE_MARK = """
WITH changed AS (
    UPDATE user_assignment_overrides so
    SET local_status = po.local_status,
        local_status_updated_at = po.local_status_updated_at,
        local_status_device_id = po.local_status_device_id,
        local_status_received_at = po.local_status_received_at,
        updated_at = now()
    FROM placeholder_merges m
    JOIN user_assignment_overrides po ON po.user_assignment_id = m.placeholder_id
    WHERE so.user_assignment_id = m.server_id
        AND NOT m.keep
        AND COALESCE(po.local_status_updated_at, '-infinity')
            > COALESCE(so.local_status_updated_at, '-infinity')
    RETURNING so.user_id, so.user_assignment_id
)
INSERT INTO placeholder_merge_changes
SELECT user_id, user_assignment_id, true FROM changed
"""

_MERGE_NOTE = """
WITH changed AS (
    UPDATE user_assignment_overrides so
    SET note = po.note,
        note_updated_at = po.note_updated_at,
        note_device_id = po.note_device_id,
        updated_at = now()
    FROM placeholder_merges m
    JOIN user_assignment_overrides po ON po.user_assignment_id = m.placeholder_id
    WHERE so.user_assignment_id = m.server_id
        AND NOT m.keep
        AND (po.note IS NOT NULL OR po.note_updated_at IS NOT NULL)
        AND (
            (so.note IS NULL AND so.note_updated_at IS NULL)
            OR COALESCE(po.note_updated_at, '-infinity')
                > COALESCE(so.note_updated_at, '-infinity')
        )
    RETURNING so.user_id, so.user_assignment_id
)
INSERT INTO placeholder_merge_changes
SELECT user_id, user_assignment_id, true FROM changed
"""

_MOVE_OVERRIDE = """
WITH changed AS (
    UPDATE user_assignment_overrides po
    SET user_assignment_id = m.server_id
    FROM placeholder_merges m
    WHERE po.user_assignment_id = m.placeholder_id
        AND NOT m.keep
        AND NOT EXISTS (
            SELECT 1 FROM user_assignment_overrides so
            WHERE so.user_assignment_id = m.server_id
        )
    RETURNING po.user_id, po.user_assignment_id
)
INSERT INTO placeholder_merge_changes
SELECT user_id, user_assignment_id, true FROM changed
"""

# The placeholder's override, where the server row had its own, goes with it.
_DROP_PLACEHOLDERS = """
DELETE FROM user_assignments p
USING placeholder_merges m
WHERE p.id = m.placeholder_id AND NOT m.keep
"""

# One change per user whose /sync/full the merge changed, so a device that
# caught up before it reads the change. A moved or winning mark or note logs
# `assignment_override`, which both apps act on with a full sync. A folded
# submission or a copied grade alone logs `assignment`, which they pass over,
# moving only their cursor: they read submissions from Moodle and use
# assignment rows only to resolve unkeyed overrides. As
# server/sync/changelog.py requires, the user's sync state is locked first
# and its watermark moves to the new entry in the same transaction.
_SYNC_STATES = """
INSERT INTO user_sync_state (user_id)
SELECT DISTINCT user_id FROM placeholder_merge_changes
ON CONFLICT (user_id) DO NOTHING
"""

_LOCK_SYNC_STATES = """
SELECT user_id FROM user_sync_state
WHERE user_id IN (SELECT user_id FROM placeholder_merge_changes)
ORDER BY user_id
FOR UPDATE
"""

_LOG_CHANGES = """
WITH per_user AS (
    SELECT
        user_id,
        CASE WHEN bool_or(override) THEN 'assignment_override' ELSE 'assignment' END
            AS entity_type,
        COALESCE(min(server_id) FILTER (WHERE override), min(server_id)) AS entity_id
    FROM placeholder_merge_changes
    GROUP BY user_id
),
appended AS (
    INSERT INTO user_change_log (user_id, entity_type, entity_id, operation)
    SELECT user_id, entity_type, entity_id::text, 'upsert' FROM per_user
    RETURNING user_id, revision
)
UPDATE user_sync_state st
SET current_revision = appended.revision,
    updated_at = now()
FROM appended
WHERE st.user_id = appended.user_id
"""


def upgrade() -> None:
    """Fold each app placeholder into the server's row of its assignment.

    Until now an app's upload went to a placeholder with course id 0 beside
    the row the server's fetch keeps under the real course id, so a
    submission or a mark the app sent sat on a row the reminder scan does
    not read. The server's row takes the submission if either row has it,
    the placeholder's grade where it has none, and the placeholder's
    override, and the placeholder is deleted. Where both rows have an
    override, the later mark and the later note win, except for a kept
    placeholder, which merges neither. Each user whose `/sync/full` that
    changes gets one change entry.

    Data only. A second run finds only the placeholders it kept, and those
    with no server row, which the next fetch takes over, and logs nothing.
    """
    for statement in (
        _PAIRS,
        _CHANGES,
        _FOLD_SUBMISSION,
        _FOLD_GRADE,
        _MERGE_MARK,
        _MERGE_NOTE,
        _MOVE_OVERRIDE,
        _DROP_PLACEHOLDERS,
        _SYNC_STATES,
        _LOCK_SYNC_STATES,
        _LOG_CHANGES,
    ):
        op.execute(statement)


def downgrade() -> None:
    """Nothing to restore: the previous release reads one row of an
    assignment as it reads two, and its next upload adds a placeholder
    again."""
