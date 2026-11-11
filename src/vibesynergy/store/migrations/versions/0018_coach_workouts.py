"""AI Coach-owned workout plans, sessions, ledger and decision log.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_plan_version",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("subject_id", sa.Integer, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("weeks_json", sa.JSON, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("safety", sa.JSON, nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer, nullable=False),
        sa.Column("framework", sa.JSON, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("approved_at", UTCDateTime),
        sa.Column("approved_by", sa.String),
        sa.Column("superseded_by", sa.String),
        sa.Column("push_state", sa.JSON),
        sa.Column("last_error", sa.Text),
    )
    op.create_index(
        "ix_coach_plan_version_subject_status",
        "coach_plan_version",
        ["subject_id", "status"],
    )
    op.create_table(
        "coach_session",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("subject_id", sa.Integer, nullable=False),
        sa.Column("plan_version_id", sa.String, sa.ForeignKey("coach_plan_version.id")),
        sa.Column("session_key", sa.String, nullable=False),
        sa.Column("local_date", sa.Date, nullable=False),
        sa.Column("sport", sa.String, nullable=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("intent", sa.String, nullable=False),
        sa.Column("hard", sa.Boolean, nullable=False),
        sa.Column("steps_json", sa.JSON, nullable=False),
        sa.Column("garmin_workout_id", sa.String),
        sa.Column("garmin_schedule_id", sa.String),
        sa.Column("schedule_date", sa.Date),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("push_checkpoint", sa.JSON, nullable=False),
        sa.Column("matched_activity_id", sa.String),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint(
            "plan_version_id",
            "session_key",
            name=op.f("uq_coach_session_plan_key"),
        ),
    )
    op.create_index("ix_coach_session_garmin_workout", "coach_session", ["garmin_workout_id"])
    op.create_index(
        "ix_coach_session_subject_state",
        "coach_session",
        ["subject_id", "state"],
    )
    op.create_table(
        "coach_workout_ledger",
        sa.Column("origin_id", sa.String, nullable=False),
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("object_kind", sa.String, nullable=False),
        sa.Column("external_id", sa.String),
        sa.Column("ledger_id", sa.String, nullable=False),
        sa.Column("intent_id", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("version_token", sa.String),
        sa.Column("session_id", sa.String, sa.ForeignKey("coach_session.id"), nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("last_action", sa.String),
        sa.Column("last_error", sa.String),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.PrimaryKeyConstraint("origin_id", "account_id", "object_kind"),
    )
    op.create_index(
        "uq_coach_workout_ledger_external",
        "coach_workout_ledger",
        ["account_id", "object_kind", "external_id"],
        unique=True,
    )
    op.create_table(
        "coach_decision_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.Integer, nullable=False),
        sa.Column("at", UTCDateTime, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("plan_version_id", sa.String),
        sa.Column("session_id", sa.String),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
    )
    op.create_index(
        "ix_coach_decision_log_subject_at",
        "coach_decision_log",
        ["subject_id", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_coach_decision_log_subject_at", table_name="coach_decision_log")
    op.drop_table("coach_decision_log")
    op.drop_index("uq_coach_workout_ledger_external", table_name="coach_workout_ledger")
    op.drop_table("coach_workout_ledger")
    op.drop_index("ix_coach_session_subject_state", table_name="coach_session")
    op.drop_index("ix_coach_session_garmin_workout", table_name="coach_session")
    op.drop_table("coach_session")
    op.drop_index("ix_coach_plan_version_subject_status", table_name="coach_plan_version")
    op.drop_table("coach_plan_version")
