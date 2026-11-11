"""Portal login: the account, sessions, one-time tokens and the brute-force brake.

Revision ID: 0047
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("password_set_at", UTCDateTime(), nullable=True),
        sa.Column("mfa", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_account")),
        sa.UniqueConstraint("email", name=op.f("uq_auth_account_email")),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("mfa_ok", sa.Boolean(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["auth_account.id"],
            name=op.f("fk_auth_session_account_id_auth_account"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
    )
    op.create_table(
        "auth_token",
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("used_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["auth_account.id"], name=op.f("fk_auth_token_account_id_auth_account")
        ),
        sa.PrimaryKeyConstraint("hash", name=op.f("pk_auth_token")),
    )
    op.create_table(
        "auth_attempt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_attempt")),
    )
    with op.batch_alter_table("auth_attempt", schema=None) as batch_op:
        batch_op.create_index("ix_auth_attempt_key_at", ["key", "at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("auth_attempt", schema=None) as batch_op:
        batch_op.drop_index("ix_auth_attempt_key_at")
    op.drop_table("auth_attempt")
    op.drop_table("auth_token")
    op.drop_table("auth_session")
    op.drop_table("auth_account")
