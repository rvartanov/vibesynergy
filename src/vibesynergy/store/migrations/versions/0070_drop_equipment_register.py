"""Drop the unused workout-equipment register (#211): ``equipment_item`` and ``equipment_location``.

The register from 0024 never got a screen or a tool; nothing read or wrote it. The Gear
register (shoes, bikes) and the coach's "Equipment" memory items are separate and stay.

The tables are dropped only when both are empty. An install that did put rows in them keeps
both tables untouched and gets a warning in the log, so no data is lost.

Revision ID: 0070
Revises: 0069
"""

import logging

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

TABLES = ("equipment_item", "equipment_location")

log = logging.getLogger("vibesynergy.store.migrations")


def upgrade() -> None:
    bind = op.get_bind()
    present = [name for name in TABLES if sa.inspect(bind).has_table(name)]
    filled = [
        name
        for name in present
        if bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first() is not None
    ]
    if filled:
        log.warning(
            "Not dropping the old equipment register: %s still has rows. "
            "The tables stay in place and are no longer used.",
            ", ".join(filled),
        )
        return
    # The item table points at the location table, so it goes first.
    for name in present:
        op.drop_table(name)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("equipment_location"):
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
    if not sa.inspect(bind).has_table("equipment_item"):
        op.create_table(
            "equipment_item",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("location_id", sa.Integer(), nullable=False),
            sa.Column("item", sa.String(), nullable=False),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("last_verified", sa.Date(), nullable=True),
            sa.Column("gear_id", sa.String(), nullable=True),
            sa.Column("garmin_catalogue_key", sa.String(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
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
        op.create_index(
            "ix_equipment_item_location_status",
            "equipment_item",
            ["location_id", "status"],
        )
