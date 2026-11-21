"""Copies removed at their source (#151).

``source_removed`` records that a source copy of an activity no longer exists where it came
from: Garmin answered 404 twice in a row for it (a failed read or write re-checked once,
or the light check during the Garmin sync). The ``activity`` row and its raw history stay
exactly as they are; the copy only drops out of the group's live sources, so publication
and the Garmin mirror pick a copy that still exists. ``removed_at`` is when VibeSynergy
found out, ``reason`` how (``write``, ``read`` or ``sync``). A copy that shows up again in
the Garmin activity list loses its row.

Revision ID: 0057
Revises: 0056
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_removed",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("removed_at", UTCDateTime(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("source", "source_id", name=op.f("pk_source_removed")),
    )


def downgrade() -> None:
    op.drop_table("source_removed")
