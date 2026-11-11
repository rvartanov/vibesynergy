"""Per destination publication checkpoints.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_ledger",
        sa.Column("source", sa.String, primary_key=True),
        sa.Column("source_id", sa.String, primary_key=True),
        sa.Column("assessment_id", sa.String, nullable=False),
        sa.Column("preview_version", sa.String(64), nullable=False),
        sa.Column("owned_block", sa.Text, nullable=False),
        sa.Column("base_description", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )
    op.create_table(
        "publication_enrichment",
        sa.Column("identity_id", sa.String, primary_key=True),
        sa.Column("plugin_id", sa.String, primary_key=True),
        sa.Column("version", sa.String, nullable=False),
        sa.Column("section", sa.Text, nullable=False),
        sa.Column("provenance", sa.JSON, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("publication_enrichment")
    op.drop_table("publication_ledger")
