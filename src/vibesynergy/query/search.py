"""One search over everything the athlete wrote or did: activities, diary notes,
coach messages, the knowledge base and gear. Each hit links to where it lives.

Plain substring search (case-insensitive) for names and texts; the knowledge base uses
its full-text index. Results are grouped by kind, newest first, a few per kind.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.store.schema import activity_unified, coach_decision_log, note

PER_KIND = 8
MIN_LENGTH = 2


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.casefold()}%"


def _snippet(text: str, query: str, width: int = 90) -> str:
    flat = " ".join((text or "").split())
    at = flat.casefold().find(query.casefold())
    if at < 0 or len(flat) <= width:
        return flat[:width]
    start = max(0, at - width // 3)
    return (
        ("…" if start else "")
        + flat[start : start + width]
        + ("…" if start + width < len(flat) else "")
    )


def search(rt: Any, query: str) -> dict[str, Any]:
    q = (query or "").strip()
    if len(q) < MIN_LENGTH:
        return {"query": q, "groups": []}
    engine: Engine = rt.engine
    pattern = _like(q)
    groups: list[dict[str, Any]] = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.name,
                activity_unified.c.local_date,
                activity_unified.c.sport,
            )
            .where(func.lower(activity_unified.c.name).like(pattern, escape="\\"))
            .order_by(activity_unified.c.local_date.desc())
            .limit(PER_KIND)
        ).all()
        groups.append(
            {
                "kind": "activities",
                "hits": [
                    {
                        "title": r.name,
                        "date": str(r.local_date),
                        "sport": r.sport,
                        "link": f"#/activity/{r.group_id}",
                    }
                    for r in rows
                ],
            }
        )
        rows = conn.execute(
            select(note.c.id, note.c.text, note.c.start_date)
            .where(func.lower(note.c.text).like(pattern, escape="\\"))
            .order_by(note.c.start_date.desc())
            .limit(PER_KIND)
        ).all()
        groups.append(
            {
                "kind": "notes",
                "hits": [
                    {
                        "title": _snippet(r.text, q),
                        "date": str(r.start_date),
                        "link": f"#/dashboard/{r.start_date}?section=diary",
                    }
                    for r in rows
                ],
            }
        )
        rows = conn.execute(
            select(
                coach_decision_log.c.id,
                coach_decision_log.c.text,
                coach_decision_log.c.for_date,
                coach_decision_log.c.at,
            )
            .where(
                func.lower(coach_decision_log.c.text).like(pattern, escape="\\"),
                coach_decision_log.c.entry_type.in_(
                    (
                        "morning",
                        "evening",
                        "note",
                        "activity_assessment",
                        "plan_change",
                        "threshold_change",
                    )
                ),
            )
            .order_by(coach_decision_log.c.at.desc())
            .limit(PER_KIND)
        ).all()
        groups.append(
            {
                "kind": "coach",
                "hits": [
                    {
                        "title": _snippet(r.text, q),
                        "date": str(r.for_date or (r.at.date() if r.at else "")),
                        "link": f"#/coach/report/{r.id}",
                    }
                    for r in rows
                ],
            }
        )
    try:
        from vibesynergy.query import knowledge

        hits = knowledge.search(engine, q, limit=PER_KIND)
    except ValueError:
        hits = []
    groups.append(
        {
            "kind": "knowledge",
            "hits": [
                {
                    "title": h.get("title") or h.get("ref"),
                    "snippet": h.get("snippet"),
                    "link": f"#/coach/knowledge/{h['article_slug']}"
                    if h.get("article_slug")
                    else "#/coach/knowledge",
                }
                for h in hits
            ],
        }
    )
    try:
        from datetime import date

        from vibesynergy.query import workouts

        gear = workouts.gear(engine, rt.raw, date.today())["gear"]
    except Exception:
        gear = []
    needle = q.casefold()
    groups.append(
        {
            "kind": "gear",
            "hits": [
                {
                    "title": " ".join(
                        str(g.get(k) or "") for k in ("name", "brand", "model")
                    ).strip(),
                    "link": "#/gear",
                }
                for g in gear
                if needle
                in " ".join(str(g.get(k) or "") for k in ("name", "brand", "model")).casefold()
            ][:PER_KIND],
        }
    )
    return {"query": q, "groups": [g for g in groups if g["hits"]]}
