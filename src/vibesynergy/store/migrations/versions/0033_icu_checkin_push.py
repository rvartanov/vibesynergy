"""Pending and completed check-in pushes to intervals.icu wellness.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "icu_checkin_push",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("checkin_updated_at", UTCDateTime(), nullable=False),
        sa.Column("pushed_at", UTCDateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("date", name=op.f("pk_icu_checkin_push")),
    )


def downgrade() -> None:
    op.drop_table("icu_checkin_push")
