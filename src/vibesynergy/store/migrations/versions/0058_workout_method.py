"""The workout method in the knowledge base (#142).

The workout library's coaching values (cadence bands, cues, % FTP and zones, warm-up and
cool-down minutes, distances, exercises, names) move from the code into the article
``workout-method``. This adds that article once, version 1, from the seed the wheel carries
(``docs/workout-method.md``), with exactly the values the code held, so no workout changes.

Nothing happens when an article ``workout-method`` already exists, or when the seed is not
found (the library then reads the same seed directly). From here on the article changes
only through the knowledge base; a later edit of the seed file does not reach it.

Revision ID: 0058
Revises: 0057
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from vibesynergy.core.ids import new_id

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

SLUG = "workout-method"
TOPIC = "workout-method"
TITLE = "Trainingsmethode: waarden van de workoutbibliotheek"
AUTHOR = "seed:docs/workout-method.md"


def upgrade() -> None:
    from vibesynergy.query import workout_method

    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM kb_article WHERE slug = :s"), {"s": SLUG}).first():
        return
    body = workout_method.seed_body()
    if not body:
        return
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    article_id = new_id("kbart")
    values = {
        "id": article_id,
        "slug": SLUG,
        "topic": TOPIC,
        "title": TITLE,
        "body": body,
        "grade": "C",
        "status": "active",
        "author": AUTHOR,
        "now": now,
    }
    conn.execute(
        sa.text(
            "INSERT INTO kb_article (id, slug, topic, title, body_md, evidence_grade, status, "
            "version, created_at, updated_at, author) VALUES (:id, :slug, :topic, :title, "
            ":body, :grade, :status, 1, :now, :now, :author)"
        ),
        values,
    )
    conn.execute(
        sa.text(
            "INSERT INTO kb_article_revision (article_id, version, slug, topic, title, body_md, "
            "evidence_grade, status, author, created_at) VALUES (:id, 1, :slug, :topic, :title, "
            ":body, :grade, :status, :author, :now)"
        ),
        values,
    )
    # The search index is derived; emptied here, it is rebuilt on the next search.
    if sa.inspect(conn).has_table("kb_fts"):
        conn.execute(sa.text("DELETE FROM kb_fts"))


def downgrade() -> None:
    conn = op.get_bind()
    article = conn.execute(
        sa.text("SELECT id FROM kb_article WHERE slug = :s AND author = :a AND version = 1"),
        {"s": SLUG, "a": AUTHOR},
    ).scalar()
    if article is None:
        return  # changed since: the knowledge base keeps it
    conn.execute(sa.text("DELETE FROM kb_article_revision WHERE article_id = :i"), {"i": article})
    conn.execute(sa.text("DELETE FROM kb_article WHERE id = :i"), {"i": article})
    if sa.inspect(conn).has_table("kb_fts"):
        conn.execute(sa.text("DELETE FROM kb_fts"))
