"""Owned Garmin calendar mirrors (CalDAV). Personal events are not stored here.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_mirror",
        sa.Column("origin_id", sa.String, nullable=False),
        sa.Column("calendar_url", sa.String, nullable=False),
        sa.Column("ledger_id", sa.String, nullable=False),
        sa.Column("intent_id", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("uid", sa.String, nullable=False),
        sa.Column("href", sa.String, nullable=False),
        sa.Column("etag", sa.String),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.String),
        sa.Column("event_date", sa.Date),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("last_action", sa.String),
        sa.Column("last_error", sa.String),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.PrimaryKeyConstraint("origin_id", "calendar_url", name=op.f("pk_calendar_mirror")),
    )
    op.create_index(
        "uq_calendar_mirror_uid",
        "calendar_mirror",
        ["calendar_url", "uid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_calendar_mirror_uid", table_name="calendar_mirror")
    op.drop_table("calendar_mirror")
