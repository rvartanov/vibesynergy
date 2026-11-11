"""Coaching load: one source, the session purpose.

An activity is a training you give (kind ``coaching``, kept out of your own load when you
choose) when its purpose is ``coached_by_me``: automatic from the title ("training geven",
``session_purpose.derive``) or chosen on the activity page. The former name rules and
coaching overrides were carried into purposes once (migration 0045) and are not read any
more. Plan workouts are recognised by the same title rule.
"""

from __future__ import annotations

from sqlalchemy import Engine, select, update

from vibesynergy.query import session_purpose
from vibesynergy.store.schema import activity_unified

COACHED = "coached_by_me"


def kind(purpose: str | None) -> str | None:
    """The activity kind for its effective purpose."""
    return "coaching" if purpose == COACHED else None


def is_coached_title(title: str | None, sport: str | None = None) -> bool:
    """A plan workout you give as a trainer (you run along easy)."""
    return session_purpose.derive(sport, title) == COACHED


def backfill(engine: Engine, *, dry_run: bool = False) -> dict[str, int]:
    """Recompute the kind from the stored purpose. Safe to repeat."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(activity_unified.c.group_id, activity_unified.c.purpose, activity_unified.c.kind)
        ).all()
        changed = 0
        coaching = 0
        for group_id, purpose, old in rows:
            new = kind(purpose)
            coaching += new == "coaching"
            if new != old:
                changed += 1
                if not dry_run:
                    conn.execute(
                        update(activity_unified)
                        .where(activity_unified.c.group_id == group_id)
                        .values(kind=new)
                    )
    return {"scanned": len(rows), "changed": changed, "coaching": coaching}


def set_override(engine: Engine, activity_id: str, is_coaching: bool | None) -> dict[str, int]:
    """Older callers (MCP ``vibesynergy_activity_set_coaching``): coaching sets the purpose
    "coached by me", not coaching sets "training", null goes back to automatic."""
    from vibesynergy.query.views import resolve_activity

    group_id = resolve_activity(engine, activity_id)
    purpose = None if is_coaching is None else (COACHED if is_coaching else "training")
    session_purpose.set_override(engine, group_id, purpose, reason="coaching classification")
    return backfill(engine)
