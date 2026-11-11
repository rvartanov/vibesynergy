"""Versioned knowledge articles, rules, sources and pending proposals.

Revision ID: 0031
Revises: 0030

The full-text index is built from these tables when the knowledge tools run.
It is not created here, so this migration stays aligned with schema.py.
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_article",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("evidence_grade", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_article")),
        sa.UniqueConstraint("slug", name=op.f("uq_kb_article_slug")),
    )
    op.create_index("ix_kb_article_topic", "kb_article", ["topic"])
    op.create_table(
        "kb_article_revision",
        sa.Column("article_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("evidence_grade", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["kb_article.id"],
            name=op.f("fk_kb_article_revision_article_id_kb_article"),
        ),
        sa.PrimaryKeyConstraint("article_id", "version", name=op.f("pk_kb_article_revision")),
    )
    op.create_table(
        "kb_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("article_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("grade", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["kb_article.id"],
            name=op.f("fk_kb_rule_article_id_kb_article"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_rule")),
    )
    op.create_index("ix_kb_rule_article_id", "kb_rule", ["article_id"])
    op.create_table(
        "kb_rule_revision",
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("grade", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["kb_article.id"],
            name=op.f("fk_kb_rule_revision_article_id_kb_article"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["kb_rule.id"],
            name=op.f("fk_kb_rule_revision_rule_id_kb_rule"),
        ),
        sa.PrimaryKeyConstraint("rule_id", "version", name=op.f("pk_kb_rule_revision")),
    )
    op.create_table(
        "kb_source",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("article_id", sa.String(), nullable=False),
        sa.Column("citation", sa.Text(), nullable=False),
        sa.Column("doi", sa.String()),
        sa.Column("url", sa.String()),
        sa.Column("verified_at", UTCDateTime()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["kb_article.id"],
            name=op.f("fk_kb_source_article_id_kb_article"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_source")),
    )
    op.create_index("ix_kb_source_article_id", "kb_source", ["article_id"])
    op.create_table(
        "kb_source_revision",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.String(), nullable=False),
        sa.Column("citation", sa.Text(), nullable=False),
        sa.Column("doi", sa.String()),
        sa.Column("url", sa.String()),
        sa.Column("verified_at", UTCDateTime()),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["kb_article.id"],
            name=op.f("fk_kb_source_revision_article_id_kb_article"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["kb_source.id"],
            name=op.f("fk_kb_source_revision_source_id_kb_source"),
        ),
        sa.PrimaryKeyConstraint("source_id", "version", name=op.f("pk_kb_source_revision")),
    )
    op.create_table(
        "kb_proposal",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("decided_at", UTCDateTime()),
        sa.Column("decided_by", sa.String()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_proposal")),
    )
    op.create_index("ix_kb_proposal_status", "kb_proposal", ["status"])


def downgrade() -> None:
    op.drop_table("kb_proposal")
    op.drop_table("kb_source_revision")
    op.drop_table("kb_source")
    op.drop_table("kb_rule_revision")
    op.drop_table("kb_rule")
    op.drop_table("kb_article_revision")
    op.drop_table("kb_article")
