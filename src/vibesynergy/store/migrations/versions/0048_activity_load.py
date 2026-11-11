"""VibeSynergy's own training load per activity, kept so it is computed once.

Revision ID: 0048
Revises: 0047
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_load",
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("inputs", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("load", sa.Float(), nullable=False),
        sa.Column("computed_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("group_id", name=op.f("pk_activity_load")),
    )


def downgrade() -> None:
    op.drop_table("activity_load")
