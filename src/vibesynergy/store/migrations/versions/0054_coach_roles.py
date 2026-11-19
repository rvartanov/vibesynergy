"""Roles per MCP connection and plan proposals (#118).

- ``coach_role``: one role per connection (``own``, ``primary`` or ``advisory``).
- ``plan_proposal``: a plan change that waits for the athlete's approval.
- Every app and token that has access now gets the role ``own``: until today a write over
  MCP counted as the athlete's own request, and that stays so for what is connected
  already. A connection made later starts as ``advisory`` until the athlete chooses.
  The AI coach inside VibeSynergy (``coach-runner``) is the primary AI coach.

Revision ID: 0054
Revises: 0053
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_role",
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_coach_role")),
    )
    op.create_table(
        "plan_proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("author_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_proposal")),
    )
    op.create_index(op.f("ix_plan_proposal_status"), "plan_proposal", ["status"], unique=False)
    conn = op.get_bind()
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    rows = [("coach-runner", "primary")]
    rows += [
        (client_id, "own")
        for (client_id,) in conn.execute(
            sa.text(
                "SELECT client_id FROM oauth_client "
                "WHERE approved_at IS NOT NULL AND revoked_at IS NULL"
            )
        )
    ]
    rows += [
        (f"pat:{token_id}", "own")
        for (token_id,) in conn.execute(sa.text("SELECT id FROM mcp_pat WHERE revoked_at IS NULL"))
    ]
    for client_id, role in rows:
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO coach_role (client_id, role, updated_at) "
                "VALUES (:client_id, :role, :now)"
            ),
            {"client_id": client_id, "role": role, "now": now},
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_plan_proposal_status"), table_name="plan_proposal")
    op.drop_table("plan_proposal")
    op.drop_table("coach_role")
