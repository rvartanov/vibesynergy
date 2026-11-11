"""Store a race priority that calendar sync does not overwrite.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "race_priority",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source", "source_id", name=op.f("pk_race_priority")),
    )


def downgrade() -> None:
    op.drop_table("race_priority")
