"""MCP tokens: expiry, scopes and display name; audit rows name the client.

Revision ID: 0025
Revises: 0024

Existing personal access tokens had no expiry. They receive 90 days from this
migration so nothing is cut off at the moment of upgrade, and they can no longer
be used without an expiry. Their scope stays the old ``vibesynergy`` name, which
means every scope except publish until the athlete consents again.
"""

from datetime import UTC, timedelta

import sqlalchemy as sa
from alembic import op

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.types import UTCDateTime

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_pat", sa.Column("expires_at", UTCDateTime(), nullable=True))
    op.add_column("mcp_pat", sa.Column("scopes", sa.JSON(), nullable=True))
    op.add_column("mcp_pat", sa.Column("display_name", sa.String(), nullable=True))
    expires = (utc_now() + timedelta(days=90)).astimezone(UTC).isoformat(timespec="microseconds")
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE mcp_pat SET scopes = '[\"vibesynergy\"]' WHERE scopes IS NULL"))
    bind.execute(
        sa.text("UPDATE mcp_pat SET expires_at = :expires WHERE expires_at IS NULL"),
        {"expires": expires},
    )
    op.add_column("audit_log", sa.Column("client_id", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("author", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("scope", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("target", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("outcome", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "ms")
    op.drop_column("audit_log", "outcome")
    op.drop_column("audit_log", "target")
    op.drop_column("audit_log", "scope")
    op.drop_column("audit_log", "author")
    op.drop_column("audit_log", "client_id")
    op.drop_column("mcp_pat", "display_name")
    op.drop_column("mcp_pat", "scopes")
    op.drop_column("mcp_pat", "expires_at")
