"""What the harness preloaded and whether the output met its contract (layer 7).

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("coach_run") as batch:
        batch.add_column(sa.Column("harness", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("coach_run") as batch:
        batch.drop_column("harness")
