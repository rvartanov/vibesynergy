"""history fills

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import vibesynergy.store.types

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "history_fill",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("start", sa.Date(), nullable=True),
        sa.Column("end", sa.Date(), nullable=True),
        sa.Column("next_month", sa.Date(), nullable=True),
        sa.Column("datasets", sa.JSON(), nullable=True),
        sa.Column("partial", sa.JSON(), nullable=False),
        sa.Column("retry", sa.JSON(), nullable=False),
        sa.Column("retried", sa.Boolean(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("wait_until", vibesynergy.store.types.UTCDateTime(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("file", sa.String(), nullable=True),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("busy_s", sa.Float(), nullable=False),
        sa.Column("created_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=False),
        sa.Column("updated_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=False),
        sa.Column("finished_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_history_fill")),
    )


def downgrade() -> None:
    op.drop_table("history_fill")
