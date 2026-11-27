"""Indexes on ``audit_log`` (#203): ``(action, target)`` and ``at``.

The daily checks, the intervals.icu notes and the advice history look a row up by its
action and target, and the old read rows are deleted by time; without an index each of
those reads the whole table. Only indexes are added: no row or column changes.

Revision ID: 0069
Revises: 0068
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_audit_log_action_target", "audit_log", ["action", "target"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action_target", table_name="audit_log")
