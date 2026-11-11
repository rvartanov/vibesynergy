"""RPE and Feel entered in VibeSynergy per activity, and their copy to intervals.icu.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_effort",
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("rpe", sa.Integer(), nullable=True),
        sa.Column("feel", sa.Integer(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("icu_activity_id", sa.String(), nullable=True),
        sa.Column("icu_pushed_at", UTCDateTime(), nullable=True),
        sa.Column("icu_error", sa.Text(), nullable=True),
        sa.Column("icu_sent", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("source_ref", name=op.f("pk_activity_effort")),
    )


def downgrade() -> None:
    op.drop_table("activity_effort")
