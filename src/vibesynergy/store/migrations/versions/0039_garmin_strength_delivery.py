"""Strength and pilates delivered to Garmin with exercise steps.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "garmin_strength_delivery",
        sa.Column("icu_event_id", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("workout_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("replaced", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("icu_event_id"),
    )


def downgrade() -> None:
    op.drop_table("garmin_strength_delivery")
