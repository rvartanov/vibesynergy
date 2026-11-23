"""A workout's own texts in VibeSynergy, with their history (#114, phase 1).

``workout_text`` has one row per version of a workout's name, public text or private text:
when, from which source (vs, strava, garmin, ai), the text before and after. Rows are only
ever added; a restore is a new row that copies an old one. One new table: no row changes.

Revision ID: 0062
Revises: 0061
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workout_text",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("old_text", sa.Text()),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("author", sa.String()),
        sa.Column("restored_from", sa.Integer()),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workout_text"),
    )
    op.create_index("ix_workout_text_group_id_field", "workout_text", ["group_id", "field"])


def downgrade() -> None:
    op.drop_index("ix_workout_text_group_id_field", table_name="workout_text")
    op.drop_table("workout_text")
