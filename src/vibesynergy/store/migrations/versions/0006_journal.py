"""journal: notes, check-ins, injuries

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25 17:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import vibesynergy.store.types

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC = vibesynergy.store.types.UTCDateTime(length=32)


def upgrade() -> None:
    op.create_table(
        "note",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("activity_id", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note")),
    )
    op.create_index("ix_note_start_date_end_date", "note", ["start_date", "end_date"], unique=False)
    op.create_index("ix_note_source_source_ref", "note", ["source", "source_ref"], unique=True)
    op.create_table(
        "checkin",
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("sleep_quality", sa.Integer(), nullable=True),
        sa.Column("soreness", sa.Integer(), nullable=True),
        sa.Column("mood", sa.Integer(), nullable=True),
        sa.Column("stress", sa.Integer(), nullable=True),
        sa.Column("motivation", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", UTC, nullable=False),
        sa.PrimaryKeyConstraint("subject_id", "date", name=op.f("pk_checkin")),
    )
    op.create_table(
        "injury",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("body_part", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("onset", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolved_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_injury")),
    )
    op.create_table(
        "injury_log",
        sa.Column("injury_id", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("pain", sa.Integer(), nullable=False),
        sa.Column("timing", sa.String(), nullable=False),
        sa.Column("affects_movement", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(
            ["injury_id"], ["injury.id"], name=op.f("fk_injury_log_injury_id_injury")
        ),
        sa.PrimaryKeyConstraint("injury_id", "date", name=op.f("pk_injury_log")),
    )


def downgrade() -> None:
    op.drop_table("injury_log")
    op.drop_table("injury")
    op.drop_table("checkin")
    op.drop_index("ix_note_source_source_ref", table_name="note")
    op.drop_index("ix_note_start_date_end_date", table_name="note")
    op.drop_table("note")
