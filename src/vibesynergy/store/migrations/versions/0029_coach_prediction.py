"""Coach predictions attached to a decision-log entry, scored from daily values.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_prediction",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("for_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("predicted", sa.Float(), nullable=False),
        sa.Column("ci_low", sa.Float()),
        sa.Column("ci_high", sa.Float()),
        sa.Column("model_id", sa.String()),
        sa.Column("actual", sa.Float()),
        sa.Column("error", sa.Float()),
        sa.Column("scored_at", UTCDateTime()),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["coach_decision_log.id"],
            name=op.f("fk_coach_prediction_entry_id_coach_decision_log"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_prediction")),
    )
    with op.batch_alter_table("coach_prediction", schema=None) as batch_op:
        batch_op.create_index("ix_coach_prediction_entry_id", ["entry_id"], unique=False)
        batch_op.create_index(
            "ix_coach_prediction_metric_for_date",
            ["metric", "for_date"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("coach_prediction")
