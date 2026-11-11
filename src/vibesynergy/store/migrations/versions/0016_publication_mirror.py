"""Strava -> Garmin publication mirror watches.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_mirror",
        sa.Column("group_id", sa.String, primary_key=True),
        sa.Column("strava_id", sa.String, nullable=False),
        sa.Column("garmin_id", sa.String),
        sa.Column("language", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("first_published_at", UTCDateTime, nullable=False),
        sa.Column("watch_until", UTCDateTime, nullable=False),
        sa.Column("next_check_at", UTCDateTime, nullable=False),
        sa.Column("last_checked_at", UTCDateTime),
        sa.Column("last_result", sa.JSON),
        sa.Column("last_error", sa.Text),
        sa.Column("garmin_original", sa.Text),
        sa.Column("garmin_original_title", sa.Text),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("publication_mirror")
