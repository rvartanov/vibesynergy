"""First observed activity delivery stages.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

activity_latency = sa.Table(
    "activity_latency",
    metadata,
    sa.Column("source", sa.String, nullable=False),
    sa.Column("source_id", sa.String, nullable=False),
    sa.Column("finished_at", UTCDateTime),
    sa.Column("source_event_at", UTCDateTime),
    sa.Column("received_at", UTCDateTime),
    sa.Column("queued_at", UTCDateTime),
    sa.Column("fetch_started_at", UTCDateTime),
    sa.Column("fetched_at", UTCDateTime),
    sa.Column("processed_at", UTCDateTime),
    sa.Column("dashboard_at", UTCDateTime),
    sa.PrimaryKeyConstraint("source", "source_id"),
    sa.Index("ix_activity_latency_finished_at", "finished_at"),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
