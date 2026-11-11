"""Push subscriptions for alerts on the phone.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("p256dh", sa.String(), nullable=False),
        sa.Column("auth", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_ok", UTCDateTime(), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("endpoint"),
    )


def downgrade() -> None:
    op.drop_table("push_subscription")
