"""Gear on the workout page, and gear synced with Strava and Garmin (#114, phase 5).

- ``gear_item``: gear of your own in VibeSynergy (a pull buoy, paddles, a wetsuit), next to
  the bikes and shoes that come from Strava.
- ``gear_map``: the one-time mapping of a VibeSynergy gear item to Garmin's gear list (a
  Strava item is its own Strava slot). A row without a Garmin uuid: "not on Garmin".
- ``activity_gear_extra``: the extra items of a workout (swim gear and other gear without a
  km total), per source copy like ``activity_gear``.
- ``gear_change``: every change of a workout's gear, from VibeSynergy, Strava or Garmin:
  only ever added.

The gear VibeSynergy last wrote to Strava and Garmin is kept with the texts' in
``text_sync.synced`` (one switch, one watch): no table for it.

New tables only: no row changes.

Revision ID: 0064
Revises: 0063
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gear_item",
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ref", name="pk_gear_item"),
    )
    op.create_table(
        "gear_map",
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("garmin_uuid", sa.String()),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ref", name="pk_gear_map"),
    )
    op.create_table(
        "activity_gear_extra",
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source_ref", "ref", name="pk_activity_gear_extra"),
    )
    op.create_table(
        "gear_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("old_refs", sa.JSON()),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("author", sa.String()),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_gear_change"),
    )
    op.create_index("ix_gear_change_group_id", "gear_change", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_gear_change_group_id", table_name="gear_change")
    op.drop_table("gear_change")
    op.drop_table("activity_gear_extra")
    op.drop_table("gear_map")
    op.drop_table("gear_item")
