"""The automatic race or test mark of an activity (#84): ``activity_purpose_override.auto_mark``.

An activity on the day of a planned test or race of its sport gets the purpose ``test`` or
``race`` by itself. ``auto_mark`` keeps what the automatic mark once set, also after the
athlete changes or undoes it, so the mark never comes back over the athlete's choice. A row
with ``auto_mark`` and no ``purpose`` is an undone mark: the title rules apply again.

Revision ID: 0068
Revises: 0067
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN: the rows that are there stay exactly as they are.
    op.add_column("activity_purpose_override", sa.Column("auto_mark", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("activity_purpose_override") as batch:
        batch.drop_column("auto_mark")
