"""intervals.icu calendar cache.

Revision ID: 0032
Revises: 0031

One row per intervals.icu event. The primary key is the intervals.icu event id.
``external_id`` is indexed. ``source`` is ``plan`` or ``other``.
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "icu_event",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("session_key", sa.String(), nullable=True),
        sa.Column("brick_part", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("workout_doc", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("for_week", sa.Boolean(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("updated", UTCDateTime(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.Column("raw_item_id", sa.String(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_item_id"],
            ["raw_item.id"],
            name=op.f("fk_icu_event_raw_item_id_raw_item"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_icu_event")),
    )
    op.create_index("ix_icu_event_date", "icu_event", ["date"])
    op.create_index("ix_icu_event_raw_item_id", "icu_event", ["raw_item_id"])
    op.create_index("ix_icu_event_external_id", "icu_event", ["external_id"])
    op.create_index("ix_icu_event_session_key", "icu_event", ["session_key"])


def downgrade() -> None:
    op.drop_index("ix_icu_event_session_key", table_name="icu_event")
    op.drop_index("ix_icu_event_external_id", table_name="icu_event")
    op.drop_index("ix_icu_event_raw_item_id", table_name="icu_event")
    op.drop_index("ix_icu_event_date", table_name="icu_event")
    op.drop_table("icu_event")
