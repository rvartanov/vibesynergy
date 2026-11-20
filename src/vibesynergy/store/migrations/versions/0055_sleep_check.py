"""The automatic sleep check per night (#100).

``sleep_check`` keeps the outcome of the plausibility check of one night (ok, suspect,
incomplete or bad, with the reasons and the metrics it touches) and when it was made. It
is a derived result: recomputed from the raw history whenever the night's inputs change
(``inputs`` is their fingerprint), never written by hand. The athlete's own choice stays in
``checkin.sleep_incomplete`` (true: incomplete, false: it's right, null: no choice).

``ix_raw_item_source_dataset_item_key`` finds every version of one item (the history of a
night that the check and ``query.measurement`` read); without it each look-up scanned the
whole table.

Revision ID: 0055
Revises: 0054
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sleep_check",
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("inputs", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("checked_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("subject_id", "date", name=op.f("pk_sleep_check")),
    )
    op.create_index(
        "ix_raw_item_source_dataset_item_key", "raw_item", ["source", "dataset", "item_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_raw_item_source_dataset_item_key", table_name="raw_item")
    op.drop_table("sleep_check")
