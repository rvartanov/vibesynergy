"""Coach plan origin: internal policy gate or external athlete fingerprint.

Revision ID: 0026
Revises: 0025

0025 is the identity migration on main. This revision adds the origin column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("coach_plan_version", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("origin", sa.String(), nullable=False, server_default="internal")
        )


def downgrade() -> None:
    with op.batch_alter_table("coach_plan_version", schema=None) as batch_op:
        batch_op.drop_column("origin")
