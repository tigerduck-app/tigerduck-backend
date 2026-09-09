"""academic_calendar

Revision ID: c8d3f1a7e2b4
Revises: b7c2e9f4a1d3
Create Date: 2026-09-08 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8d3f1a7e2b4'
down_revision: str | Sequence[str] | None = 'b7c2e9f4a1d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Semester date ranges, school holidays, and per-user exceptions.

    Seeds nothing: an empty calendar is a meaningful state the clients
    already handle by failing open (nothing suppressed), and guessing the
    current term here would put the same hardcoded date this feature exists
    to remove into the database instead of the app.
    """
    op.create_table(
        "semester_terms",
        sa.Column("code", sa.String(length=8), primary_key=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("name_en", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("end_date >= start_date", name="chk_semester_term_range"),
    )

    op.create_table(
        "academic_holidays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name_zh", sa.String(length=128), nullable=False),
        sa.Column("name_en", sa.String(length=128), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("end_date >= start_date", name="chk_holiday_range"),
    )
    # The clients ask "which holidays touch this window" on every reschedule.
    op.create_index(
        "ix_academic_holidays_start", "academic_holidays", ["start_date"]
    )

    op.create_table(
        "user_holiday_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "holiday_id",
            sa.Integer(),
            sa.ForeignKey("academic_holidays.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "notify", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "holiday_id", name="ux_user_holiday_override"),
    )
    op.create_index(
        "ix_user_holiday_overrides_user", "user_holiday_overrides", ["user_id"]
    )

    # The changelog gates entity_type with a CHECK, so a new synced entity
    # has to be admitted here or every override write fails at commit.
    op.drop_constraint("chk_changelog_entity_type", "user_change_log", type_="check")
    op.create_check_constraint(
        "chk_changelog_entity_type",
        "user_change_log",
        "entity_type IN ("
        "'course', 'course_override', 'course_skipped_date', "
        "'assignment', 'assignment_override', "
        "'settings_document', "
        "'bulletin_subscription', 'bulletin_state', 'bulletin_match', "
        "'holiday_override')",
    )


def downgrade() -> None:
    # Drop the rows first: the narrowed constraint would reject them, and a
    # CHECK is validated against existing data when it is added.
    op.execute("DELETE FROM user_change_log WHERE entity_type = 'holiday_override'")
    op.drop_constraint("chk_changelog_entity_type", "user_change_log", type_="check")
    op.create_check_constraint(
        "chk_changelog_entity_type",
        "user_change_log",
        "entity_type IN ("
        "'course', 'course_override', 'course_skipped_date', "
        "'assignment', 'assignment_override', "
        "'settings_document', "
        "'bulletin_subscription', 'bulletin_state', 'bulletin_match')",
    )
    op.drop_index("ix_user_holiday_overrides_user", table_name="user_holiday_overrides")
    op.drop_table("user_holiday_overrides")
    op.drop_index("ix_academic_holidays_start", table_name="academic_holidays")
    op.drop_table("academic_holidays")
    op.drop_table("semester_terms")
