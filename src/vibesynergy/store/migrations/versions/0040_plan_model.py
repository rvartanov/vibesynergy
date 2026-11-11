"""The training plan as VibeSynergy's own model.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=True),
        sa.Column("start", sa.Date(), nullable=True),
        sa.Column("end", sa.Date(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "plan_version",
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "n"),
    )
    op.create_table(
        "plan_week",
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("monday", sa.Date(), nullable=False),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("hours_label", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "n"),
    )
    op.create_table(
        "plan_target",
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(), nullable=False),
        sa.Column("time_s", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("load", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "week", "sport"),
    )
    op.create_table(
        "plan_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot", sa.String(), nullable=True),
        sa.Column("sport", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("workout", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plan_session_plan_date", "plan_session", ["plan_id", "date"])
    op.create_table(
        "plan_link",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("remote_id", sa.String(), nullable=True),
        sa.Column("remote_version", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("synced_at", UTCDateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id", "target"),
    )


def downgrade() -> None:
    op.drop_table("plan_link")
    op.drop_index("ix_plan_session_plan_date", table_name="plan_session")
    op.drop_table("plan_session")
    op.drop_table("plan_target")
    op.drop_table("plan_week")
    op.drop_table("plan_version")
    op.drop_table("plan")
