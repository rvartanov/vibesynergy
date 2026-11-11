"""Coachplatform slice 1: coach_decision_log author model and session purpose.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("coach_decision_log", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("author", sa.String(), nullable=False, server_default="legacy")
        )
        batch_op.add_column(
            sa.Column("entry_type", sa.String(), nullable=False, server_default="note")
        )
        batch_op.add_column(sa.Column("idempotency_key", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("for_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("valid_until", UTCDateTime(), nullable=True))
        batch_op.create_index(
            "uq_coach_decision_log_idempotency_key",
            ["idempotency_key"],
            unique=True,
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )
    op.execute(
        sa.text(
            "UPDATE coach_decision_log SET author = 'legacy' WHERE author IS NULL OR author = ''"
        )
    )
    with op.batch_alter_table("coach_decision_log", schema=None) as batch_op:
        batch_op.alter_column("author", server_default=None)

    op.create_table(
        "activity_purpose_override",
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("source_ref", name=op.f("pk_activity_purpose_override")),
    )
    op.add_column("activity_unified", sa.Column("purpose", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("activity_unified", "purpose")
    op.drop_table("activity_purpose_override")
    with op.batch_alter_table("coach_decision_log", schema=None) as batch_op:
        batch_op.drop_index("uq_coach_decision_log_idempotency_key")
        batch_op.drop_column("valid_until")
        batch_op.drop_column("for_date")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("entry_type")
        batch_op.drop_column("author")
