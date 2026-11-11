"""Coachplatform slice 2 fix: optional reason on activity purpose overrides.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_purpose_override", sa.Column("reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("activity_purpose_override", "reason")
