"""Model-neutral names: the slug ``ai-coach-loop`` and the note source ``ai`` (#96).

No name in VibeSynergy says which AI model or provider does the coaching.

- The knowledge-base article ``claude-coach-loop`` becomes ``ai-coach-loop``: the article,
  its stored versions (the change log reads the slug from them) and the proposals still
  waiting for it, so they apply to the renamed article. The old slug stays an alias in the
  code (``knowledge.SLUG_ALIASES``); the article's text is not touched.
- Diary notes an AI wrote before per-app identities carry ``source = "claude"``; that
  becomes ``"ai"``. The code still reads the old value.

Both steps do nothing on a second run, and nothing when an ``ai-coach-loop`` article already
exists (the old article then stays as it is, for the athlete to look at).

Revision ID: 0052
Revises: 0051
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

OLD_SLUG, NEW_SLUG = "claude-coach-loop", "ai-coach-loop"
OLD_SOURCE, NEW_SOURCE = "claude", "ai"


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def _rename_slug(conn, old: str, new: str) -> None:
    taken = conn.execute(sa.text("SELECT 1 FROM kb_article WHERE slug = :s"), {"s": new}).first()
    article = conn.execute(
        sa.text("SELECT id FROM kb_article WHERE slug = :s"), {"s": old}
    ).scalar()
    if taken or article is None:
        return
    conn.execute(
        sa.text("UPDATE kb_article SET slug = :new WHERE id = :id"), {"new": new, "id": article}
    )
    conn.execute(
        sa.text(
            "UPDATE kb_article_revision SET slug = :new WHERE article_id = :id AND slug = :old"
        ),
        {"new": new, "old": old, "id": article},
    )
    # Proposals still waiting keep their base version and text; only the target moves.
    conn.execute(
        sa.text(
            "UPDATE kb_proposal SET target = :new "
            "WHERE kind = 'article' AND target = :old AND status = 'pending'"
        ),
        {"new": new, "old": old},
    )
    pending = conn.execute(
        sa.text(
            "SELECT id, changes FROM kb_proposal "
            "WHERE kind IN ('rule', 'source') AND status = 'pending'"
        )
    ).all()
    for proposal_id, raw in pending:
        changes = _load(raw)
        if isinstance(changes, dict) and changes.get("article_slug") == old:
            changes["article_slug"] = new
            conn.execute(
                sa.text("UPDATE kb_proposal SET changes = :c WHERE id = :id"),
                {"c": json.dumps(changes), "id": proposal_id},
            )
    # The search index is derived; emptied here, it is rebuilt on the next search.
    if sa.inspect(conn).has_table("kb_fts"):
        conn.execute(sa.text("DELETE FROM kb_fts"))


def upgrade() -> None:
    conn = op.get_bind()
    _rename_slug(conn, OLD_SLUG, NEW_SLUG)
    conn.execute(
        sa.text("UPDATE note SET source = :new WHERE source = :old"),
        {"new": NEW_SOURCE, "old": OLD_SOURCE},
    )


def downgrade() -> None:
    conn = op.get_bind()
    _rename_slug(conn, NEW_SLUG, OLD_SLUG)
    conn.execute(
        sa.text("UPDATE note SET source = :old WHERE source = :new"),
        {"new": NEW_SOURCE, "old": OLD_SOURCE},
    )
