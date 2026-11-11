"""training calendar and plans

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import vibesynergy.store.types

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _provenance() -> list:
    return [
        sa.Column("raw_item_id", sa.String(), nullable=False),
        sa.Column("updated_at", vibesynergy.store.types.UTCDateTime(length=32), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "planned_item",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("sport", sa.String(), nullable=True),
        sa.Column("plan_id", sa.String(), nullable=True),
        sa.Column("is_race", sa.Boolean(), nullable=False),
        sa.Column("primary", sa.Boolean(), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        *_provenance(),
        sa.ForeignKeyConstraint(
            ["raw_item_id"], ["raw_item.id"], name=op.f("fk_planned_item_raw_item_id_raw_item")
        ),
        sa.PrimaryKeyConstraint("source", "source_id", name=op.f("pk_planned_item")),
    )
    with op.batch_alter_table("planned_item", schema=None) as batch_op:
        batch_op.create_index("ix_planned_item_date", ["date"], unique=False)
        batch_op.create_index("ix_planned_item_raw_item_id", ["raw_item_id"], unique=False)
    op.create_table(
        "training_plan",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("plan_type", sa.String(), nullable=True),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        *_provenance(),
        sa.ForeignKeyConstraint(
            ["raw_item_id"], ["raw_item.id"], name=op.f("fk_training_plan_raw_item_id_raw_item")
        ),
        sa.PrimaryKeyConstraint("source", "source_id", name=op.f("pk_training_plan")),
    )
    with op.batch_alter_table("training_plan", schema=None) as batch_op:
        batch_op.create_index("ix_training_plan_raw_item_id", ["raw_item_id"], unique=False)


def downgrade() -> None:
    op.drop_table("training_plan")
    op.drop_table("planned_item")
