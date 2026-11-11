"""API coach runner: one row per run (usage, cost, report) and the athlete's replies.

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(), nullable=False),
        sa.Column("run_key", sa.String(), nullable=False),
        sa.Column("for_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("stop_reason", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("mailed_at", UTCDateTime(), nullable=True),
        sa.Column("mail_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_run")),
        sa.UniqueConstraint("job", "run_key", name="uq_coach_run_job_run_key"),
    )
    op.create_index("ix_coach_run_started_at", "coach_run", ["started_at"])
    op.create_table(
        "coach_reply",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("read_at", UTCDateTime(), nullable=True),
        sa.Column("read_run_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_reply")),
    )
    op.create_index("ix_coach_reply_entry_id", "coach_reply", ["entry_id"])
    op.create_index("ix_coach_reply_read_at", "coach_reply", ["read_at"])


def downgrade() -> None:
    op.drop_index("ix_coach_reply_read_at", table_name="coach_reply")
    op.drop_index("ix_coach_reply_entry_id", table_name="coach_reply")
    op.drop_table("coach_reply")
    op.drop_index("ix_coach_run_started_at", table_name="coach_run")
    op.drop_table("coach_run")
