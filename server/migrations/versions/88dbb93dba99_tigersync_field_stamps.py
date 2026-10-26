"""Every synced override value has its own stamp, a course name one per locale

Revision ID: 88dbb93dba99
Revises: 5d2a9c7e4f1b
Create Date: 2026-10-26 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '88dbb93dba99'
down_revision: str | Sequence[str] | None = '5d2a9c7e4f1b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A timestamp as a TigerSync clock value, integer microseconds since the epoch
# (`server/sync/merge.py`). `extract` answers a numeric, so this is exact.
_CLOCK = "(extract(epoch FROM {ts}) * 1000000)::bigint"


def upgrade() -> None:
    """Give each stored value the stamp the per-field merge compares.

    A value whose stamp is missing takes its row's `updated_at`, the last time
    the row changed, so an edit made since outranks it and one made before
    does not. Each locale of a course name takes the name's stamp, the one
    stamp the map had until now. `updated_at` itself is left as it is.

    The column is added without rewriting the table; the updates touch only
    rows with a value and no stamp. A second run, after a rollback that kept
    the column, raises each locale's stamp to the name stamp the previous
    release moved, and changes nothing else that already has a stamp.
    """
    op.execute(
        "ALTER TABLE user_course_overrides ADD COLUMN IF NOT EXISTS"
        " custom_names_updated_at JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    for table, field, has_value in (
        ("user_course_overrides", "color_hex", "color_hex IS NOT NULL"),
        ("user_course_overrides", "custom_name", "custom_names <> '{}'::jsonb"),
        ("user_course_overrides", "ignore_assignments", "ignore_assignments"),
        ("user_assignment_overrides", "local_status", "local_status <> 'none'"),
        ("user_assignment_overrides", "note", "note IS NOT NULL"),
    ):
        op.execute(
            f"UPDATE {table} SET {field}_updated_at = updated_at"
            f" WHERE {field}_updated_at IS NULL AND {has_value}"
        )
    name_stamp = _CLOCK.format(ts="o.custom_name_updated_at")
    op.execute(
        f"""
        UPDATE user_course_overrides o SET custom_names_updated_at = (
            SELECT jsonb_object_agg(
                n.key,
                GREATEST(COALESCE((o.custom_names_updated_at ->> n.key)::bigint, 0), {name_stamp})
            )
            FROM jsonb_object_keys(o.custom_names) AS n(key)
        )
        WHERE o.custom_names <> '{{}}'::jsonb
          AND o.custom_name_updated_at IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM jsonb_object_keys(o.custom_names) AS n(key)
            WHERE COALESCE((o.custom_names_updated_at ->> n.key)::bigint, 0) < {name_stamp}
          )
        """
    )


def downgrade() -> None:
    """The previous release stamps a name once for every locale.

    The stamps filled in for values that had none stay: the previous release
    compares them the way it compares any stamp.
    """
    op.execute(
        "ALTER TABLE user_course_overrides DROP COLUMN IF EXISTS custom_names_updated_at"
    )
