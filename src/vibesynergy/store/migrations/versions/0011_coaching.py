"""Add durable coaching classification and name rules.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_unified", sa.Column("kind", sa.String(), nullable=True))
    op.create_table(
        "coaching_override",
        sa.Column("source_ref", sa.String(), primary_key=True),
        sa.Column("is_coaching", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "coaching_rule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("match_type", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("coaching_rule")
    op.drop_table("coaching_override")
    op.drop_column("activity_unified", "kind")
