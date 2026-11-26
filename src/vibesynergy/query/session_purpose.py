"""Session purpose tag: training, recovery, leisure, coached_by_me, race, test.

The purpose comes from the title (``derive``), from the athlete's choice (an override row per
source) or from the automatic race/test mark (#84, ``race_test_marks``): an activity on the
day of a planned test or race of its sport. The automatic mark is an override row with
``auto_mark`` set; ``auto_mark`` stays when the athlete changes or undoes it (``purpose``
NULL), so the mark is never set again over the athlete's choice.
"""

from __future__ import annotations

import re
from typing import Literal

from sqlalchemy import Engine, delete, select, update

from vibesynergy.store.schema import activity_purpose_override, activity_unified

Purpose = Literal["training", "recovery", "leisure", "coached_by_me", "race", "test"]
PURPOSES = frozenset({"training", "recovery", "leisure", "coached_by_me", "race", "test"})
# The purposes a threshold calculation reads (``threshold_tests``).
MARKS = frozenset({"race", "test"})
COACHED = "coached_by_me"
AUTO_REASON = "auto: "  # the reason of an automatic mark starts with this
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
    """The chosen purpose per source; an undone automatic mark (no purpose) is not a choice."""
    return {
        source_ref: purpose
        for source_ref, purpose in conn.execute(
            select(
                activity_purpose_override.c.source_ref, activity_purpose_override.c.purpose
            ).where(activity_purpose_override.c.purpose.is_not(None))
        )
    }


def _rows_for(conn, sources: list[str]) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            select(activity_purpose_override).where(
                activity_purpose_override.c.source_ref.in_(sources)
            )
        ).mappings()
    ]


def mark_state(conn, sources: list[str]) -> dict[str, object]:
    """How the activity's purpose was set: ``auto_mark`` (what the automatic mark once set,
    or None), ``automatic`` (that mark is still in place) and ``undone`` (the athlete
    removed it)."""
    rows = _rows_for(conn, sources)
    auto = next((r["auto_mark"] for r in rows if r.get("auto_mark")), None)
    automatic = any(
        r.get("auto_mark")
        and r["purpose"] == r["auto_mark"]
        and str(r.get("reason") or "").startswith(AUTO_REASON)
        for r in rows
    )
    return {
        "auto_mark": auto,
        "automatic": automatic,
        "undone": bool(auto) and not automatic,
        "chosen": bool(rows) and not automatic and any(r["purpose"] for r in rows),
    }


def effective(
    sport: str | None,
    name: str | None,
    sources: list[str],
    overrides: dict[str, str | None],
) -> str | None:
    manual = [overrides[key] for key in sources if overrides.get(key)]
    if manual:
        return manual[0]
    return derive(sport, name)


def _validate(purpose: str | None) -> str | None:
    if purpose is None:
        return None
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(sorted(PURPOSES))} or null")
    return purpose


def _sources(engine: Engine, activity_id: str) -> list[str]:
    with engine.connect() as conn:
        row = conn.execute(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == activity_id)
        ).first()
    if not row:
        raise LookupError("activity not found")
    return list(row[0] or [])


def set_override(
    engine: Engine,
    activity_id: str,
    purpose: str | None,
    *,
    reason: str | None = None,
) -> dict[str, int]:
    """The athlete's choice (null: the automatic rules). An automatic race or test mark that
    the athlete changes or removes is kept as undone, so it never comes back by itself."""
    purpose = _validate(purpose)
    sources = _sources(engine, activity_id)
    with engine.begin() as conn:
        auto = next((r["auto_mark"] for r in _rows_for(conn, sources) if r.get("auto_mark")), None)
        conn.execute(
            delete(activity_purpose_override).where(
                activity_purpose_override.c.source_ref.in_(sources)
            )
        )
        if purpose is not None or auto:
            conn.execute(
                activity_purpose_override.insert(),
                [
                    {"source_ref": source, "purpose": purpose, "reason": reason, "auto_mark": auto}
                    for source in sources
                ],
            )
    return backfill(engine)


def set_auto_mark(engine: Engine, activity_id: str, purpose: str, *, reason: str) -> bool:
    """The automatic race or test mark (#84). Only on an activity without any purpose row:
    never over the athlete's choice, and never again after the athlete changed or undid it.
    True when the mark was set. The caller runs ``backfill``."""
    if purpose not in MARKS:
        raise ValueError(f"an automatic mark is one of {', '.join(sorted(MARKS))}")
    sources = _sources(engine, activity_id)
    with engine.begin() as conn:
        if _rows_for(conn, sources):
            return False
        conn.execute(
            activity_purpose_override.insert(),
            [
                {
                    "source_ref": source,
                    "purpose": purpose,
                    "reason": AUTO_REASON + reason,
                    "auto_mark": purpose,
                }
                for source in sources
            ],
        )
    return True


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
