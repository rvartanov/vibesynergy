"""One place for what waits on the athlete: decisions first, then notices.

Decisions are proposals only the athlete can take or leave: knowledge-base changes from
the coach (Coach → Change log) and new threshold values (Training → Zones & thresholds).
Notices need no decision but are worth a look: a coach routine that did not run
and a plan horizon that is not on Garmin as it should be. Each item links to the
page where it is handled, so a proposal handled there leaves this list by itself. The
portal shows the decision count next to the bell in the header.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import athlete_threshold


def _kb(engine: Engine) -> list[dict[str, Any]]:
    from vibesynergy.query import knowledge

    out = []
    for p in knowledge.pending_proposals(engine):
        current = p.get("current") or {}
        changes = p.get("changes") or {}
        citation = changes.get("citation") or current.get("citation") or ""
        out.append(
            {
                "kind": "kb_proposal",
                "id": p.get("id"),
                "target": current.get("title")
                or citation[:80]
                or current.get("slug")
                or p.get("target"),
                "what": p.get("kind"),
                "why": p.get("rationale"),
                "at": p.get("created_at"),
                "link": "#/coach/knowledge",
            }
        )
    return out


def _thresholds(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                athlete_threshold.c.id,
                athlete_threshold.c.sport,
                athlete_threshold.c.metric,
                athlete_threshold.c.value,
                athlete_threshold.c.unit,
                athlete_threshold.c.created_at,
            ).where(athlete_threshold.c.status == "proposed")
        ).all()
    return [
        {
            "kind": "threshold_proposal",
            "id": r.id,
            "sport": r.sport,
            "metric": r.metric,
            "value": r.value,
            "unit": r.unit,
            "at": r.created_at.isoformat() if r.created_at else None,
            "link": "#/zones",
        }
        for r in rows
    ]


def _notices(rt: Any, today: date) -> list[dict[str, Any]]:
    from vibesynergy.query import coach_tasks, horizon

    out: list[dict[str, Any]] = []
    try:
        for task in coach_tasks.status(rt.engine, utc_now(), rt.settings.timezone):
            for day in task["missing"]:
                out.append(
                    {
                        "kind": "coach_missed",
                        "task": task["title"],
                        "date": day,
                        "link": "#/coach/tasks",
                    }
                )
    except Exception:  # a notice must never break the list
        pass
    try:
        from vibesynergy.portal import prefs

        stored = prefs.load(rt.settings.data_dir)
        guard = horizon.status(
            rt.engine,
            today,
            prefs.horizon_days(rt.settings.data_dir),
            tuple(stored.get("garmin_delivery") or ()),
        )
        for key in ("missing", "duplicates", "outdated"):
            for item in guard.get(key) or []:
                out.append(
                    {
                        "kind": f"horizon_{key}",
                        "title": item["title"],
                        "date": item["date"],
                        "link": "#/planning",
                    }
                )
        if (guard.get("fill") or {}).get("behind"):
            out.append(
                {
                    "kind": "horizon_behind",
                    "date": guard["fill"].get("planned_through") or guard["from"],
                    "end": guard["through"],
                    "link": "#/planning",
                }
            )
    except Exception:
        pass
    return out


def inbox(rt: Any, today: date) -> dict[str, Any]:
    decisions = _kb(rt.engine) + _thresholds(rt.engine)
    return {"decisions": decisions, "notices": _notices(rt, today), "count": len(decisions)}


def count(engine: Engine) -> int:
    """The number next to the bell: decisions only, cheap enough for every page."""
    return len(_kb(engine)) + len(_thresholds(engine))
