"""Time in zones per activity as VibeSynergy counts it from the streams, kept so it is
computed once.

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_zone_own",
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("inputs", sa.String(), nullable=False),
        sa.Column("zones", sa.JSON(), nullable=False),
        sa.Column("computed_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "kind", name=op.f("pk_activity_zone_own")),
    )


def downgrade() -> None:
    op.drop_table("activity_zone_own")
