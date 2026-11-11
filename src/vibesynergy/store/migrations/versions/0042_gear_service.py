"""Maintenance per gear item: what, how often, last done.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gear_service",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("what", sa.String(), nullable=False),
        sa.Column("every_km", sa.Float(), nullable=True),
        sa.Column("every_days", sa.Integer(), nullable=True),
        sa.Column("last_done", sa.Date(), nullable=True),
        sa.Column("last_km", sa.Float(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gear_service_ref", "gear_service", ["ref"])


def downgrade() -> None:
    op.drop_index("ix_gear_service_ref", table_name="gear_service")
    op.drop_table("gear_service")
