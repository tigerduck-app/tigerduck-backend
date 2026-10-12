"""Course overrides can ignore every assignment of the course

Revision ID: ddf188f91a15
Revises: f3c8a1d5b927
Create Date: 2026-10-13 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ddf188f91a15'
down_revision: Union[str, Sequence[str], None] = 'f3c8a1d5b927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEVICE_FK = "user_course_overrides_ignore_assignments_device_id_fkey"


def upgrade() -> None:
    """Let a user ignore all of a course's assignments, and say which
    Moodle course that is.

    `ignore_assignments` is a merge triplet like `color_hex`: the value, when
    it was set and by which device. While it is true the reminder scan skips
    the course's assignments and cancels what it filed for them. It finds the
    ones the server fetched by `moodle_course_id`, so until a client has sent
    that it skips only the placeholders the apps upload.

    `moodle_course_id` is the course's numeric Moodle id. The override is
    reached by the course's Moodle idnumber, which the assignments the server
    fetches from Moodle do not carry, so the scan needs the number the client
    sends alongside. Nullable: a client may not know it yet.

    Backfilled to false, which is what every existing override meant.

    Runs again over columns that already exist: rolling back to 3.2.0
    stamps the database to f3c8a1d5b927 and keeps them (MIGRATE.md), and
    the next start of this release upgrades again.
    """
    op.execute(
        "ALTER TABLE user_course_overrides"
        " ADD COLUMN IF NOT EXISTS ignore_assignments BOOLEAN NOT NULL DEFAULT false,"
        " ADD COLUMN IF NOT EXISTS ignore_assignments_updated_at TIMESTAMP WITH TIME ZONE,"
        " ADD COLUMN IF NOT EXISTS ignore_assignments_device_id UUID,"
        " ADD COLUMN IF NOT EXISTS moodle_course_id BIGINT"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{_DEVICE_FK}'
                    AND conrelid = 'user_course_overrides'::regclass
            ) THEN
                ALTER TABLE user_course_overrides
                    ADD CONSTRAINT {_DEVICE_FK}
                    FOREIGN KEY (ignore_assignments_device_id)
                    REFERENCES user_devices (id) ON DELETE SET NULL;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    """Destroys every course's ignore setting and numeric Moodle id. While
    the columns are gone the full sync carries no `ignore_assignments` and
    the apps keep their own setting, but the reminder scan no longer honours
    it; an upgrade adds the columns back as false, the apps take that false
    on their next full sync, and the settings are gone for good.
    """
    op.execute(
        "ALTER TABLE user_course_overrides"
        f" DROP CONSTRAINT IF EXISTS {_DEVICE_FK},"
        " DROP COLUMN IF EXISTS moodle_course_id,"
        " DROP COLUMN IF EXISTS ignore_assignments_device_id,"
        " DROP COLUMN IF EXISTS ignore_assignments_updated_at,"
        " DROP COLUMN IF EXISTS ignore_assignments"
    )
