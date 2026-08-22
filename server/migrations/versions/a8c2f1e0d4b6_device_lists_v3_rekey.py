"""Re-key device_list_members onto v3 user_devices.

The operator device-lists feature stored membership as a FK to the v2
``device_registrations.device_id`` (a string). After the device-UUID
migration the apps register into ``user_devices`` and the v2 table is
abandoned, so membership must reference ``user_devices.id`` (UUID).

Existing membership rows point at dead v2 device_ids, so we truncate
before re-typing the column. Small user base / clean break — no attempt
to map old rows forward.

Revision ID: a8c2f1e0d4b6
Revises: f7e3a6d9c2b1
Create Date: 2026-06-18
"""
from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a8c2f1e0d4b6"
down_revision = "f7e3a6d9c2b1"
branch_labels = None
depends_on = None

_FK = "device_list_members_device_id_fkey"


def upgrade() -> None:
    # Rows reference v2 device_ids that no longer exist; drop them.
    op.execute("TRUNCATE device_list_members")
    op.drop_constraint(_FK, "device_list_members", type_="foreignkey")
    op.alter_column(
        "device_list_members",
        "device_id",
        type_=postgresql.UUID(as_uuid=True),
        postgresql_using="device_id::uuid",
        existing_nullable=False,
    )
    op.create_foreign_key(
        _FK,
        "device_list_members",
        "user_devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.execute("TRUNCATE device_list_members")
    op.drop_constraint(_FK, "device_list_members", type_="foreignkey")
    op.alter_column(
        "device_list_members",
        "device_id",
        type_=postgresql.VARCHAR(length=128),
        postgresql_using="device_id::text",
        existing_nullable=False,
    )
    op.create_foreign_key(
        _FK,
        "device_list_members",
        "device_registrations",
        ["device_id"],
        ["device_id"],
        ondelete="CASCADE",
    )
