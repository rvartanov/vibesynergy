"""Personal access tokens for MCP (AI agents).

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_pat",
        sa.Column("id", sa.String, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("last_used_at", UTCDateTime),
        sa.Column("revoked_at", UTCDateTime),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_pat")),
        sa.UniqueConstraint("name", name=op.f("uq_mcp_pat_name")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_mcp_pat_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("mcp_pat")
