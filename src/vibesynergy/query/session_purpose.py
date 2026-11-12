"""Session purpose tag: training, recovery, leisure, coached_by_me, race."""

from __future__ import annotations

import re
from typing import Literal

from sqlalchemy import Engine, delete, select, update

from vibesynergy.store.schema import activity_purpose_override, activity_unified

Purpose = Literal["training", "recovery", "leisure", "coached_by_me", "race"]
PURPOSES = frozenset({"training", "recovery", "leisure", "coached_by_me", "race"})
_RACE = re.compile(
    r"\b(race|wedstrijd|ironman|triathlon|championship|kampioenschap)\b"
    r"|"
    r"\bmarathon\s+build\b"
    r"|"
    r"\bmarathon\b(?!\s+build\b)",
    re.IGNORECASE,
)
_RECOVERY = re.compile(r"\b(recovery|herstel|rest day|rustdag)\b", re.IGNORECASE)
_LEISURE = re.compile(r"\b(leisure|recreatief|fun swim|zwemmen recreatief)\b", re.IGNORECASE)
_COACHED = re.compile(r"training geven", re.IGNORECASE)
_TRAINING = re.compile(
    r"\b(interval|tempo|threshold|vo2|werk|training|fartlek|progression)\b",
    re.IGNORECASE,
)


def derive(sport: str | None, name: str | None) -> str | None:
    title = (name or "").strip()
    if not title:
        return None
    if _COACHED.search(title):
        return "coached_by_me"
    if _RACE.search(title):
        return "race"
    if _RECOVERY.search(title):
        return "recovery"
    if _LEISURE.search(title):
        return "leisure"
    if _TRAINING.search(title):
        return "training"
    sport_key = (sport or "").lower()
    if sport_key in {"strength", "pilates", "yoga"} and len(title) > 3:
        return "training"
    return None


def _overrides(conn) -> dict[str, str | None]:
    return {
        source_ref: purpose
        for source_ref, purpose in conn.execute(
            select(activity_purpose_override.c.source_ref, activity_purpose_override.c.purpose)
        )
    }


def effective(
    sport: str | None,
    name: str | None,
    sources: list[str],
    overrides: dict[str, str | None],
) -> str | None:
    manual = [overrides[key] for key in sources if key in overrides]
    if manual:
        return manual[0]
    return derive(sport, name)


def _validate(purpose: str | None) -> str | None:
    if purpose is None:
        return None
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(sorted(PURPOSES))} or null")
    return purpose


def set_override(
    engine: Engine,
    activity_id: str,
    purpose: str | None,
    *,
    reason: str | None = None,
) -> dict[str, int]:
    purpose = _validate(purpose)
    with engine.connect() as conn:
        row = conn.execute(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == activity_id)
        ).first()
    if not row:
        raise LookupError("activity not found")
    sources = list(row[0] or [])
    with engine.begin() as conn:
        conn.execute(
            delete(activity_purpose_override).where(
                activity_purpose_override.c.source_ref.in_(sources)
            )
        )
        if purpose is not None:
            conn.execute(
                activity_purpose_override.insert(),
                [
                    {"source_ref": source, "purpose": purpose, "reason": reason}
                    for source in sources
                ],
            )
    return backfill(engine)


def backfill(engine: Engine, *, dry_run: bool = False) -> dict[str, int]:
    with engine.begin() as conn:
        overrides = _overrides(conn)
        rows = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.sport,
                activity_unified.c.name,
                activity_unified.c.sources,
                activity_unified.c.purpose,
                activity_unified.c.kind,
            )
        ).all()
        changed = 0
        for group_id, sport, name, sources, old, old_kind in rows:
            new = effective(sport, name, sources, overrides)
            # The coaching kind follows the purpose.
            new_kind = "coaching" if new == "coached_by_me" else None
            if new != old or new_kind != old_kind:
                changed += 1
                if not dry_run:
                    conn.execute(
                        update(activity_unified)
                        .where(activity_unified.c.group_id == group_id)
                        .values(purpose=new, kind=new_kind)
                    )
    return {"scanned": len(rows), "changed": changed}
