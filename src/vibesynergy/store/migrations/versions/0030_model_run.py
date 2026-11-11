"""Personal model runs: parameters, report, and a diff against the previous run.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_run",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_model_run_model_id_started_at",
        "model_run",
        ["model_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_run_model_id_started_at", table_name="model_run")
    op.drop_table("model_run")
