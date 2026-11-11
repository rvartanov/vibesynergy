"""Morning decisions and evening reviews, one stored row per day, kind and input version.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

daily_review = sa.Table(
    "daily_review",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("subject_id", sa.Integer, nullable=False),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("input_version", sa.String(64), nullable=False),
    sa.Column("inputs", sa.JSON, nullable=False),
    sa.Column("safety", sa.JSON, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("interpretation", sa.JSON),
    sa.Column("model", sa.JSON),
    sa.Column("previous_id", sa.String),
    sa.Column("changed", sa.JSON),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("completed_at", UTCDateTime),
    sa.Column("error", sa.String),
    sa.Index(
        "uq_daily_review_day_kind_input",
        "subject_id",
        "local_date",
        "kind",
        "input_version",
        unique=True,
    ),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
