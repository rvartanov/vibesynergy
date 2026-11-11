"""Gear per activity and default gear per sport.

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_gear",
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source_ref"),
    )
    op.create_table(
        "gear_default",
        sa.Column("sport", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("since", sa.Date(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("sport"),
    )


def downgrade() -> None:
    op.drop_table("gear_default")
    op.drop_table("activity_gear")
