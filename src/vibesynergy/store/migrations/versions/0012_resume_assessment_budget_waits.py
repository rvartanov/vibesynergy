"""Release assessments waiting on the removed daily AI-call cap.

Revision ID: 0012
Revises: 0011

Coach settings stay JSON. A stored assessments.daily_limit key is ignored by the
policy model and is not rewritten here, so historical fingerprints stay stable.
"""

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The daily AI-call cap and the assessment worker are gone. Nothing to resume.
    pass


def downgrade() -> None:
    # The cap is not restored. Revisions already returned to pending stay pending.
    pass
