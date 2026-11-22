"""Races and events in VibeSynergy (#91): ``race.kind`` and ``race.end_date``.

The ``race`` table also holds the other events that matter to the plan: a group training, a
camp, a holiday or a time you are not available. ``kind`` says which (``race`` for every
row that is there, so nothing changes for them); ``end_date`` is the last day of an event
that lasts more than one day. Two columns only: no row changes.

Revision ID: 0060
Revises: 0059
"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("race") as batch:
        batch.add_column(sa.Column("kind", sa.String(), nullable=False, server_default="race"))
        batch.add_column(sa.Column("end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("race") as batch:
        batch.drop_column("end_date")
        batch.drop_column("kind")
