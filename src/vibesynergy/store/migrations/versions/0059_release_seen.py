"""Versions and "What changed" (#140): ``release_seen``.

The last version whose "What changed" an account saw (``vibesynergy.releases``), so the
card after a release shows once. A new table only: nothing that is there changes. The key
is the account id, or 0 on an install without a portal account (login off).

Revision ID: 0059
Revises: 0058
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_seen",
        sa.Column("account_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("seen_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("release_seen")
