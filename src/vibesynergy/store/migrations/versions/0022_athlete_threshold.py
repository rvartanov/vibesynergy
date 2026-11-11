"""Coachplatform slice 2: athlete_threshold store for confirmed training thresholds.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "athlete_threshold",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sport", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("source_device", sa.String()),
        sa.Column("confidence", sa.Float()),
        sa.Column("date_set", sa.Date()),
        sa.Column("next_review", sa.Date()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("superseded_by", sa.Integer()),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["athlete_threshold.id"],
            name=op.f("fk_athlete_threshold_superseded_by_athlete_threshold"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_athlete_threshold")),
    )
    with op.batch_alter_table("athlete_threshold", schema=None) as batch_op:
        batch_op.create_index(
            "ix_athlete_threshold_sport_metric_status",
            ["sport", "metric", "status"],
            unique=False,
        )
        batch_op.create_index(
            "uq_athlete_threshold_current_sport_metric",
            ["sport", "metric"],
            unique=True,
            sqlite_where=sa.text("status = 'current'"),
        )


def downgrade() -> None:
    op.drop_table("athlete_threshold")
