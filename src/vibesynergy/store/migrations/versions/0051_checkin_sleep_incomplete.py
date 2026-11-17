"""Check-in: the night's sleep measurement marked incomplete (#53).

The watch can stop recording mid-night (17-11: 3 h 29 min measured, almost 12 h slept). The
athlete marks that night, and the short sleep and the readiness computed from it no longer
count for the day's advice. Garmin's record stays as it is; unmarking undoes it.

Revision ID: 0051
Revises: 0050
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("checkin") as batch:
        batch.add_column(sa.Column("sleep_incomplete", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("checkin") as batch:
        batch.drop_column("sleep_incomplete")
