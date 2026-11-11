"""Persist calendar mirror write intent before provider requests.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calendar_mirror", sa.Column("pending_write", sa.JSON))


def downgrade() -> None:
    op.drop_column("calendar_mirror", "pending_write")
