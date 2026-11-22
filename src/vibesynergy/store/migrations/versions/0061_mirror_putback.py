"""Another app overwriting the plan (#110): ``mirror_putback`` and ``mirror_hold``.

``mirror_putback`` has one row per copy that another app or device changed in a mirror
(intervals.icu, Garmin, the calendar) and VibeSynergy put back; ``mirror_hold`` the mirrors
it stopped putting back until the athlete confirms. Two new tables: no row changes.

Revision ID: 0061
Revises: 0060
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mirror_putback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mirror", sa.String(), nullable=False),
        sa.Column("item", sa.String(), nullable=False),
        sa.Column("name", sa.String()),
        sa.Column("run", sa.String(), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.Column("changed_at", UTCDateTime()),
        sa.Column("writer", sa.String()),
        sa.Column("put_back", sa.Boolean(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name="pk_mirror_putback"),
    )
    op.create_index("ix_mirror_putback_mirror_at", "mirror_putback", ["mirror", "at"])
    op.create_table(
        "mirror_hold",
        sa.Column("mirror", sa.String(), nullable=False),
        sa.Column("held_at", UTCDateTime()),
        sa.Column("reset_at", UTCDateTime()),
        sa.Column("reset_by", sa.String()),
        sa.PrimaryKeyConstraint("mirror", name="pk_mirror_hold"),
    )


def downgrade() -> None:
    op.drop_table("mirror_hold")
    op.drop_index("ix_mirror_putback_mirror_at", table_name="mirror_putback")
    op.drop_table("mirror_putback")
