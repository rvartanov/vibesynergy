"""Versioned coaching context and configuration; no external actions.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_head",
        sa.Column("subject_id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_version", sa.Integer()),
    )
    op.create_table(
        "coach_revision",
        sa.Column("subject_id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("effective", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
    )
    op.create_table(
        "coach_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("coach_event")
    op.drop_table("coach_revision")
    op.drop_table("coach_head")
