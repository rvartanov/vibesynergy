"""Preloaded context for harness tasks (layer 3).

Every task gets the fixed base (``registry.BASE_CONTEXT``) plus its own chain. Each block is
built from ``vibesynergy.query`` only, has a character budget and a version, and never fails
the run: a block that cannot be built says so in one line and the model can still fetch it
through its tools.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from vibesynergy.coach_runner import registry
from vibesynergy.core.timeutil import Window

log = logging.getLogger(__name__)

VERSION = 1


@dataclass(frozen=True)
class Block:
    name: str
    title: str
    budget: int  # characters of JSON
    build: Callable[[Any, date, dict[str, Any]], Any]
    version: int = 1


DESCRIPTION_CHARS = 700  # a session's step text in a block; the tools have the rest


def _short(text: Any) -> Any:
    if isinstance(text, str) and len(text) > DESCRIPTION_CHARS:
        return text[:DESCRIPTION_CHARS] + " …"
    return text


def _volume(found: dict[str, Any]) -> list[dict[str, Any]]:
    """Volume buckets, compact: hours and km per period and sport. A multisport holder is
    left out, its parts count, so a brick is not counted twice."""
    from vibesynergy.query.multisport import is_multisport

    return [
        {
            "period": b.get("period"),
            "sport": b.get("sport"),
            "n": b.get("count"),
            "h": round((b.get("moving_s") or 0) / 3600, 2),
            "km": round((b.get("distance_m") or 0) / 1000, 1),
        }
        for b in found.get("buckets") or []
        if not is_multisport(b.get("sport"))
    ]


def _series_ends(points: Any) -> Any:
    """A daily series as where it started and ended in the window, and how many points."""
    if not isinstance(points, list) or not points:
        return points
    first, last = points[0], points[-1]
    return {"from": first, "to": last, "n": len(points)}


def _brief(rt, day: date, cache: dict[str, Any]) -> dict[str, Any]:
    if "brief" not in cache:
        from vibesynergy.query import coach_brief

        cache["brief"] = coach_brief.today_brief(rt, day)
    return cache["brief"]


def _athlete(rt, day, cache):
    from vibesynergy.query import coach

    return coach.athlete_for_coach(rt.engine, rt.settings)


def _communication(rt, day, cache):
    return (
        "Advies eerst, gewone coachtaal, hoogstens één kanttekening, cijfers vertaald naar "
        "betekenis, kort. Bronnen en herkomst alleen intern."
    )


def _safety(rt, day, cache):
    from vibesynergy.query import journal

    return [
        {
            "title": row.get("title"),
            "status": row.get("status"),
            "latest_pain": row.get("latest_pain"),
            "warnings": row.get("warnings"),
        }
        for row in journal.injuries(rt.engine, day, days=14)
    ]


def _memory(rt, day, cache):
    from vibesynergy.query import coach

    active = coach.state(rt.engine, "context").get("active") or {}
    return [
        {"kind": row.get("kind"), "text": row.get("text"), "days": row.get("days") or []}
        for row in (active.get("payload") or {}).get("constraints") or []
        if row.get("active", True)
    ]


def _today(rt, day, cache):
    brief = _brief(rt, day, cache)
    keys = ("date", "checkin", "complaints", "pain_today", "load", "today", "tomorrow")
    return {k: brief.get(k) for k in keys} | {"race": brief.get("race")}


def _recovery(rt, day, cache):
    return _brief(rt, day, cache).get("recovery")


def _plan_horizon(rt, day, cache):
    from vibesynergy.query import plan_model

    found = plan_model.view(rt.engine, day, day + timedelta(days=8))
    return {
        "version": found.get("version"),
        "sessions": [
            {
                **{k: s.get(k) for k in ("date", "sport", "name", "status")},
                "description": _short(s.get("description")),
            }
            for s in found.get("sessions") or []
        ],
        "weeks": [
            {k: w.get(k) for k in ("monday", "phase", "note")} for w in found.get("weeks") or []
        ],
        "guard": _brief(rt, day, cache).get("horizon"),
    }


def _plan_next_week(rt, day, cache):
    from vibesynergy.query import plan_model

    monday = day + timedelta(days=7 - day.weekday())
    found = plan_model.view(rt.engine, monday, monday + timedelta(days=6))
    return [
        {k: s.get(k) for k in ("date", "sport", "name", "status")}
        for s in found.get("sessions") or []
    ]


def _week(rt, day, cache):
    from vibesynergy.query import coach_brief

    monday = day - timedelta(days=day.weekday())
    found = coach_brief.week_brief(rt, monday, monday + timedelta(days=6), day)
    found.pop("progress", None)  # per-day detail; the items and totals carry the week
    return found


def _weather(rt, day, cache):
    from vibesynergy.query import activity_weather

    found = activity_weather.forecast(rt)
    found["days"] = (found.get("days") or [])[:9]  # the plan horizon
    return found


def _replies(rt, day, cache):
    from vibesynergy.query import coach_replies

    return [
        {"entry_id": r["entry_id"], "at": r["at"], "text": r["text"]}
        for r in coach_replies.unread(rt.engine)
    ]


def _recent_reports(rt, day, cache):
    from vibesynergy.query import predictions

    rows = predictions.coach_entries(rt.engine, Window(day - timedelta(days=6), day))
    return [
        {
            "for_date": r.get("for_date"),
            "entry_type": r.get("entry_type"),
            "text": str(r.get("text") or "")[:400],
        }
        for r in rows[:6]
    ]


def _fitness(rt, day, cache):
    from vibesynergy.query import training

    found = training.fitness(rt.engine, Window(day - timedelta(days=27), day))
    return {
        group: {name: _series_ends(points) for name, points in (values or {}).items()}
        for group, values in found.items()
    }


def _month_bounds(day: date) -> tuple[date, date, date]:
    """The month the report is about (the one before ``day``'s), and the one before that."""
    end = day.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    before = (start - timedelta(days=1)).replace(day=1)
    return before, start, end


def _month(rt, day, cache):
    from vibesynergy.query import training

    before, start, end = _month_bounds(day)
    return {
        "month": start.strftime("%Y-%m"),
        "this": _volume(training.volume(rt.engine, Window(start, end), granularity="month")),
        "previous": _volume(
            training.volume(
                rt.engine, Window(before, start - timedelta(days=1)), granularity="month"
            )
        ),
    }


def _block(rt, day, cache):
    """The phase that ends: its weeks with targets, the volume done per week, and the
    first weeks of the next phase."""
    from vibesynergy.query import plan_model, training

    found = plan_model.view(rt.engine, day - timedelta(days=7 * 16), day + timedelta(days=21))
    weeks = sorted(found.get("weeks") or [], key=lambda w: w["monday"])
    past = [w for w in weeks if w["monday"] <= day.isoformat()]
    if not past:
        return None
    phase = past[-1].get("phase")
    block = []
    for w in reversed(past):
        if w.get("phase") != phase:
            break
        block.insert(0, w)
    start = date.fromisoformat(block[0]["monday"])
    return {
        "phase": phase,
        "weeks": [
            {k: w.get(k) for k in ("monday", "kind", "hours", "note", "targets")} for w in block
        ],
        "done": _volume(training.volume(rt.engine, Window(start, day), granularity="week")),
        "next": [
            {k: w.get(k) for k in ("monday", "phase", "kind", "hours", "note")}
            for w in weeks
            if w["monday"] > day.isoformat()
        ],
    }


def _threshold_signals(rt, day, cache):
    from vibesynergy.query import thresholds

    return {
        "signals": thresholds.signals(rt.engine, day),
        "current": thresholds.current_values(rt.engine),
    }


KB_PER_MONTH = 5


def _kb_due(rt, day, cache):
    """The articles longest without an update (five a month) and what is already pending."""
    from vibesynergy.query import knowledge

    active = [a for a in knowledge.list_articles(rt.engine) if a["status"] == "active"]
    due = sorted(active, key=lambda a: a.get("updated_at") or "")[:KB_PER_MONTH]
    return {
        "check": due,
        "pending": [
            {k: p.get(k) for k in ("id", "kind", "target", "summary")}
            for p in knowledge.pending_proposals(rt.engine)
        ],
        "articles": len(active),
    }


def _races(rt, day, cache):
    from vibesynergy.query import plan

    return plan.calendar_races(rt.engine, day)


def _activity(rt, day, cache):
    from vibesynergy.query import views

    ids = cache.get("activity_ids") or []
    # A brick: the holder and its parts, so up to five.
    return [views.activity_detail(rt.engine, rt.parquet, str(i)) for i in ids[:5]] or None


def _plan_match(rt, day, cache):
    from vibesynergy.query import plan_model

    found = plan_model.view(rt.engine, day, day)
    return [
        {
            **{k: s.get(k) for k in ("session_id", "sport", "name", "status")},
            "description": _short(s.get("description")),
        }
        for s in found.get("sessions") or []
    ]


BLOCKS: dict[str, Block] = {
    b.name: b
    for b in (
        Block("athlete", "Atleet", 3000, _athlete),
        Block("communication_rule", "Communicatieregel", 400, _communication),
        Block("safety", "Blessures en klachten", 1500, _safety),
        Block("coach_memory", "Coachgeheugen", 2000, _memory),
        Block("today", "Vandaag", 3000, _today),
        Block("recovery", "Herstel", 1200, _recovery),
        Block("plan_horizon", "Plan komende 9 dagen", 10000, _plan_horizon),
        Block("plan_next_week", "Plan volgende week", 2500, _plan_next_week),
        Block("week", "Deze week", 6000, _week),
        Block("weather", "Weer", 2000, _weather),
        Block("replies", "Antwoorden op eerdere rapporten", 3000, _replies),
        Block("recent_reports", "Recente rapporten", 3000, _recent_reports),
        Block("fitness", "Fitheid", 2500, _fitness),
        Block("activity", "Activiteit", 4000, _activity),
        Block("plan_match", "Gepland op die dag", 2500, _plan_match),
        Block("month", "Vorige maand", 4000, _month),
        Block("races", "Wedstrijden", 1500, _races),
        Block("kb_due", "Kennisbank: te controleren", 3000, _kb_due),
        Block("threshold_signals", "Drempelsignalen", 4000, _threshold_signals),
        Block("block", "Blok dat eindigt", 6000, _block),
    )
}


def _render(block: Block, data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > block.budget:
        text = text[: block.budget] + " …(ingekort; haal de rest op met de tools)"
    return text


def preload(rt, task_id: str, day: date, **extra: Any) -> dict[str, Any]:
    """The task's context, block by block: ``{"text", "blocks": [{name, version, chars, ok}]}``.

    Unknown chain names (a block not built yet) and failing blocks are listed, not raised.
    """
    cache: dict[str, Any] = dict(extra)
    parts: list[str] = []
    report: list[dict[str, Any]] = []
    for name in registry.chain_for(task_id):
        block = BLOCKS.get(name)
        if block is None:
            report.append({"name": name, "version": None, "chars": 0, "ok": False})
            continue
        try:
            data = block.build(rt, day, cache)
            ok = True
        except Exception as exc:  # one block never sinks the run
            log.warning("context block %s failed: %s", name, exc)
            data = f"Niet beschikbaar ({type(exc).__name__}); gebruik de tools."
            ok = False
        if data in (None, [], {}):
            data = "geen"
        text = _render(block, data)
        parts.append(
            f'<context name="{name}" v="{block.version}">\n{block.title}: {text}\n</context>'
        )
        report.append({"name": name, "version": block.version, "chars": len(text), "ok": ok})
    return {"version": VERSION, "text": "\n".join(parts), "blocks": report}
