"""One place for what waits on the athlete: decisions first, then notices.

Decisions are proposals only the athlete can take or leave: plan changes from an AI coach
(approved or rejected in the list itself, #118), knowledge-base changes from
the coach (Coach → Change log) and new threshold values (Training → Zones & thresholds).
Notices need no decision but are worth a look: a coach routine that did not run,
a plan horizon that is not on Garmin as it should be, and the knowledge-base texts
VibeSynergy fixed itself (#137). What changed per version (#140) is listed with the notices.
Each item links to the
page where it is handled, so a proposal handled there leaves this list by itself. The
portal shows the decision count next to the bell in the header.
"""

from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

from sqlalchemy import Engine, select

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
                # What changes, not the whole article (#137).
                "diff": p.get("diff") or [],
                "link": "#/coach/knowledge",
            }
        )
    return out


def _editorial(engine: Engine) -> list[dict[str, Any]]:
    """One line for the texts VibeSynergy fixed without asking (#137): how many, and what
    kind of fix. The list with an undo per fix is on Coach → Methods & rules."""
    from vibesynergy.query import kb_fix, knowledge

    fixed = knowledge.editorial_changes(engine, days=knowledge.NOTICE_DAYS)
    if not fixed:
        return []
    checks = {fix["check"] for item in fixed for fix in item["fixes"]}
    return [
        {
            "kind": "kb_editorial",
            "count": len({(item["kind"], item["target"]) for item in fixed}),
            "checks": [check for check in kb_fix.FIXABLE if check in checks],
            "at": fixed[0]["at"],
            "link": "#/coach/methods",
        }
    ]


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


def _plan_proposals(engine: Engine) -> list[dict[str, Any]]:
    """Plan changes an AI coach may not make by itself (#118): approved or rejected here."""
    from vibesynergy.query import plan_proposals

    return [
        {
            "kind": "plan_proposal",
            "id": p["id"],
            "role": p["role"],
            "author": p["author"],
            "author_name": p["author_name"],
            "change": p["summary"],
            "why": p["reason"],
            "at": p["created_at"],
            "link": "#/inbox",
        }
        for p in plan_proposals.pending(engine)
    ]


def _disagreements(rt: Any, today: date) -> list[dict[str, Any]]:
    """A second opinion that differs from the primary AI coach's advice for today or
    tomorrow in safety level or a session decision (#118)."""
    from datetime import timedelta

    from vibesynergy.query import daily_advice

    out = []
    for day in (today, today + timedelta(days=1)):
        for opinion in daily_advice.thread_opinions(rt, day):
            if opinion["disagrees"]:
                out.append(
                    {
                        "kind": "coach_disagreement",
                        "date": day.isoformat(),
                        "author_name": opinion["author_name"],
                        "differences": opinion["differences"],
                        "link": "#/coach",
                    }
                )
    return out


def _notices(rt: Any, today: date) -> list[dict[str, Any]]:
    from vibesynergy.query import coach_tasks, horizon

    out: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):  # a notice must never break the list
        out.extend(_disagreements(rt, today))
    with contextlib.suppress(Exception):
        out.extend(_editorial(rt.engine))
    try:
        for task in coach_tasks.status_for(rt):
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
        from vibesynergy.query import garmin_strength

        stored = prefs.load(rt.settings.data_dir)
        guard = horizon.status(
            rt.engine,
            today,
            prefs.horizon_days(rt.settings.data_dir),
            tuple(stored.get("garmin_delivery") or ()),
            mark=garmin_strength.marks_from(stored),
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
    decisions = _plan_proposals(rt.engine) + _kb(rt.engine) + _thresholds(rt.engine)
    from vibesynergy.query import release_news

    return {
        "decisions": decisions,
        "notices": _notices(rt, today),
        "count": len(decisions),
        # What changed per version (#140): after the card, it stays readable here.
        "releases": release_news.history(),
    }


def count(engine: Engine) -> int:
    """The number next to the bell: decisions only, cheap enough for every page."""
    return len(_plan_proposals(engine)) + len(_kb(engine)) + len(_thresholds(engine))
