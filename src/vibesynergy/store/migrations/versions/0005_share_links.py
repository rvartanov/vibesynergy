"""share links

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25 16:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import vibesynergy.store.types

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "share_link",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("pages", sa.JSON(), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        sa.Column("created_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=False),
        sa.Column("expires_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=True),
        sa.Column("revoked_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=True),
        sa.Column("last_used_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_share_link")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_share_link_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("share_link")
