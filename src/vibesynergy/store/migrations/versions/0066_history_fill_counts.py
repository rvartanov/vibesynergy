"""What an archive import brought in, per kind (``history_fill.counts``).

The Strava account archive also brings the workout photos: ``counts`` keeps how many files
were checked and how many photos were added (``{"files": 7300, "photos": 312}``), so the
import's result can say both. Imports from before this column stay as they are (null).

Revision ID: 0066
Revises: 0065
"""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN: the rows that are there stay exactly as they are.
    op.add_column("history_fill", sa.Column("counts", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("history_fill") as batch:
        batch.drop_column("counts")
