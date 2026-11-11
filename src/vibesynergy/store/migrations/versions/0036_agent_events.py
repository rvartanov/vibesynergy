"""Agent events: debounce watch, events and the webhook delivery outbox.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_event_watch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("due_at", UTCDateTime(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_event_watch")),
    )
    op.create_index("ix_coach_event_watch_first_seen_at", "agent_event_watch", ["first_seen_at"])
    op.create_table(
        "agent_event",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_event")),
    )
    op.create_index("ix_coach_event_occurred_at", "agent_event", ["occurred_at"])
    op.create_table(
        "agent_event_delivery",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("subscription_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("next_at", UTCDateTime(), nullable=False),
        sa.Column("attempted_at", UTCDateTime(), nullable=True),
        sa.Column("sent_at", UTCDateTime(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("link", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_event_delivery")),
        sa.UniqueConstraint(
            "event_id",
            "subscription_id",
            name="uq_coach_event_delivery_event_id_subscription_id",
        ),
    )
    op.create_index(
        "ix_coach_event_delivery_state_next_at", "agent_event_delivery", ["state", "next_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_coach_event_delivery_state_next_at", table_name="agent_event_delivery")
    op.drop_table("agent_event_delivery")
    op.drop_index("ix_coach_event_occurred_at", table_name="agent_event")
    op.drop_table("agent_event")
    op.drop_index("ix_coach_event_watch_first_seen_at", table_name="agent_event_watch")
    op.drop_table("agent_event_watch")
