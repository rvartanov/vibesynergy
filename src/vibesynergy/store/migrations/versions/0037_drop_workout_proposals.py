"""Drop the workout-proposal tables.

VibeSynergy's own propose → approve → Garmin push route is gone; the plan flows
intervals.icu → Garmin. The three tables were never written on the server. The upgrade
refuses to drop a table that still holds rows, so nothing is lost silently.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

# Children first: the ledger points at sessions, sessions at plan versions.
TABLES = ("coach_workout_ledger", "coach_session", "coach_plan_version")


def upgrade() -> None:
    conn = op.get_bind()
    existing = set(sa.inspect(conn).get_table_names())
    counts = {
        name: conn.execute(sa.text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
        for name in TABLES
        if name in existing
    }
    filled = {name: count for name, count in counts.items() if count}
    if filled:
        listed = ", ".join(f"{name} ({count} rows)" for name, count in filled.items())
        raise RuntimeError(
            "Refusing to drop the workout-proposal tables: they still hold data: "
            f"{listed}. Export or archive those rows first, then run the upgrade again."
        )
    for name in TABLES:
        if name in existing:
            op.drop_table(name)


def downgrade() -> None:
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
        sa.Column("origin", sa.String, nullable=False, server_default="internal"),
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
    op.create_index("ix_coach_session_subject_state", "coach_session", ["subject_id", "state"])
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
