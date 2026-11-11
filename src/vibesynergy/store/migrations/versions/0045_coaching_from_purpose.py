"""Coaching load follows the session purpose: carry the old outcome over once.

Until now an activity's kind (``coaching``) came from name rules and coaching overrides,
and its purpose separately from the title. From now on the kind follows the purpose. So
that the coaching load stays exactly the same, every activity whose stored kind and
purpose disagree gets a purpose override with the old outcome: ``coached_by_me`` where it
counted as coaching, ``training`` where a "training geven" title did not. The rule and
override tables stay (unused) for a possible downgrade.

Revision ID: 0045
Revises: 0044
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

REASON = "carried over from the coaching classification"


def upgrade() -> None:
    conn = op.get_bind()
    unified = sa.table(
        "activity_unified",
        sa.column("group_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("purpose", sa.String),
        sa.column("sources", sa.JSON),
    )
    override = sa.table(
        "activity_purpose_override",
        sa.column("source_ref", sa.String),
        sa.column("purpose", sa.String),
        sa.column("reason", sa.Text),
        sa.column("updated_at", sa.String),
    )
    rows = conn.execute(
        sa.select(unified.c.group_id, unified.c.kind, unified.c.purpose, unified.c.sources)
    ).all()
    now = datetime.now(UTC).isoformat()
    columns = {c["name"] for c in sa.inspect(conn).get_columns("activity_purpose_override")}
    for group_id, kind, purpose, sources in rows:
        coached = kind == "coaching"
        if coached == (purpose == "coached_by_me"):
            continue
        want = "coached_by_me" if coached else "training"
        refs = list(sources or [])
        if not refs:
            continue
        conn.execute(override.delete().where(override.c.source_ref.in_(refs)))
        values = []
        for ref in refs:
            row = {"source_ref": ref, "purpose": want, "reason": REASON}
            if "updated_at" in columns:
                row["updated_at"] = now
            values.append(row)
        conn.execute(override.insert(), values)
        conn.execute(unified.update().where(unified.c.group_id == group_id).values(purpose=want))


def downgrade() -> None:
    # The kind is recomputed from the old rules on the next rebuild after a downgrade.
    pass
