"""Two-way sync of a published workout's name and public text (#114, phase 3).

``text_sync`` has one row per published workout that VibeSynergy keeps in step with Strava
and Garmin. Per platform it holds the last name and public text VibeSynergy wrote there (or
found there and agreed with): the third side of the three-way compare, so a write of
VibeSynergy's own never comes back as an outside change. One new table: no row changes.

Revision ID: 0063
Revises: 0062
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "text_sync",
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("strava_id", sa.String()),
        sa.Column("garmin_id", sa.String()),
        sa.Column("synced", sa.JSON(), nullable=False),
        sa.Column("noticed", sa.JSON(), nullable=False),
        sa.Column("watch_until", UTCDateTime(), nullable=False),
        sa.Column("next_check_at", UTCDateTime()),
        sa.Column("seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_checked_at", UTCDateTime()),
        sa.Column("last_result", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("group_id", name="pk_text_sync"),
    )
    op.create_index("ix_text_sync_state_next_check_at", "text_sync", ["state", "next_check_at"])


def downgrade() -> None:
    op.drop_index("ix_text_sync_state_next_check_at", table_name="text_sync")
    op.drop_table("text_sync")
