"""Coachplatform slice 3: equipment register (locations and items).

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipment_location",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_location")),
        sa.UniqueConstraint("key", name=op.f("uq_equipment_location_key")),
    )
    op.create_table(
        "equipment_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("item", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String()),
        sa.Column("last_verified", sa.Date()),
        sa.Column("gear_id", sa.String()),
        sa.Column("garmin_catalogue_key", sa.String()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["equipment_location.id"],
            name=op.f("fk_equipment_item_location_id_equipment_location"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_item")),
        sa.UniqueConstraint(
            "location_id",
            "item",
            "category",
            name=op.f("uq_equipment_item_location_item_category"),
        ),
    )
    with op.batch_alter_table("equipment_item", schema=None) as batch_op:
        batch_op.create_index(
            "ix_equipment_item_location_status",
            ["location_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("equipment_item")
    op.drop_table("equipment_location")
