"""Editorial knowledge-base fixes without approval (#137).

``kb_proposal.editorial`` keeps the server's check of a proposal: whether every change is a
fix of a ``knowledge.lint`` warning (an old name, a rule range, a tool name, a fixed
threshold now read live), which fixes, and why a proposal waits when it is not. A pure fix
is applied at once (status ``applied``, a new version by ``lint/editorial``). Proposals made
before this column stay as they are (null).

Revision ID: 0056
Revises: 0055
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN: the rows that are there stay exactly as they are.
    op.add_column("kb_proposal", sa.Column("editorial", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("kb_proposal") as batch:
        batch.drop_column("editorial")
