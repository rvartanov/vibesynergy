"""Own settings per gear item: status, lifetime limit, note.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gear_setting",
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("life_km", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ref"),
    )


def downgrade() -> None:
    op.drop_table("gear_setting")
