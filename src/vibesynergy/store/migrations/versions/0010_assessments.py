"""Durable shared activity assessment identity, snapshots and decisions.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

assessment_identity = sa.Table(
    "assessment_identity",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("subject_id", sa.Integer, nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False),
)
assessment_alias = sa.Table(
    "assessment_alias",
    metadata,
    sa.Column("subject_id", sa.Integer, primary_key=True),
    sa.Column("source_ref", sa.String, primary_key=True),
    sa.Column("identity_id", sa.String, sa.ForeignKey("assessment_identity.id"), nullable=False),
)
assessment_revision = sa.Table(
    "assessment_revision",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("identity_id", sa.String, sa.ForeignKey("assessment_identity.id"), nullable=False),
    sa.Column("input_version", sa.String(64), nullable=False),
    sa.Column("inputs", sa.JSON, nullable=False),
    sa.Column("public_facts", sa.JSON, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("interpretation", sa.JSON),
    sa.Column("model", sa.JSON),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("completed_at", UTCDateTime),
    sa.Column("error", sa.String),
    sa.Index("uq_assessment_identity_input", "identity_id", "input_version", unique=True),
)
assessment_head = sa.Table(
    "assessment_head",
    metadata,
    sa.Column("identity_id", sa.String, sa.ForeignKey("assessment_identity.id"), primary_key=True),
    sa.Column("revision_id", sa.String, sa.ForeignKey("assessment_revision.id"), nullable=False),
)
assessment_event = sa.Table(
    "assessment_event",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("identity_id", sa.String, sa.ForeignKey("assessment_identity.id"), nullable=False),
    sa.Column("revision_id", sa.String, sa.ForeignKey("assessment_revision.id"), nullable=False),
    sa.Column("previous_id", sa.String),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
