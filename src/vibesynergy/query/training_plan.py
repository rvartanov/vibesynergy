"""The whole intervals.icu training plan for the Training plan page (read-only).

Weeks come from the weekly NOTE events (``Wk 7 · Base 1 · raceweek · ~6u``) and the weekly
TARGET events (time per sport). The next nine days list the plan workouts in full. Races
are the same merged list ``plan`` uses. Edits stay in intervals.icu and the coach flow.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, or_, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import icu_plan
from vibesynergy.query import plan as plan_q
from vibesynergy.query.race_priority import stored_priorities
from vibesynergy.store.schema import activity_unified, icu_event, planned_item

UPCOMING_DAYS = 9
# The kind of week, from the note title, in the plan's own words (Dutch or English).
_KINDS = (
    ("recovery", ("herstel", "recovery", "rest")),
    ("taper", ("taper",)),
    ("race", ("race",)),
    ("load", ("laad", "load", "build week")),
)
_NUMBER = re.compile(r"\b(?:wk|week)\s*(\d+)\b", re.IGNORECASE)


def week_kind(text: str | None) -> str | None:
    """load / recovery / race / taper from a part such as ``herstelweek``."""
    lowered = (text or "").casefold()
    return next((kind for kind, words in _KINDS if any(w in lowered for w in words)), None)


def parse_title(title: str | None) -> dict[str, Any]:
    """``Wk 7 · Base 1 · raceweek · ~6u`` -> number 7, phase Base 1, kind race, hours ~6u."""
    parts = [part.strip() for part in re.split(r"\s[·|]\s", title or "") if part.strip()]
    number = None
    if parts and (match := _NUMBER.search(parts[0])):
        number = int(match.group(1))
        parts = parts[1:]
    kind = None
    kind_index = None
    # The part that names the week ("herstelweek") wins over a phase called "Herstel".
    ranked = sorted(enumerate(parts), key=lambda pair: "week" not in pair[1].casefold())
    for index, part in ranked:
        if found := week_kind(part):
            kind, kind_index = found, index
            break
    phase = parts[0] if parts and kind_index != 0 and not parts[0].startswith("~") else None
    hours = next((p for p in parts if p.startswith("~")), None)
    return {"number": number, "phase": phase, "kind": kind, "hours_label": hours}


def overview(engine: Engine, today: date) -> dict[str, Any]:
    monday = today - timedelta(days=today.weekday())
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                select(icu_event)
                .where(icu_event.c.category.in_(("NOTE", "TARGET")), icu_event.c.for_week)
                .order_by(icu_event.c.date, icu_event.c.id)
            ).mappings()
        ]
        garmin_races = [
            dict(row)
            for row in conn.execute(
                select(planned_item)
                .where(
                    planned_item.c.kind == "event",
                    planned_item.c.date >= today,
                    or_(planned_item.c.is_race, planned_item.c.primary),
                )
                .order_by(planned_item.c.date)
            ).mappings()
        ]
    notes = {row["date"]: row for row in rows if row["category"] == "NOTE"}
    targets: dict[date, list[dict]] = defaultdict(list)
    for row in rows:
        if row["category"] == "TARGET":
            targets[row["date"]].append(row)
    mondays = sorted(set(notes) | set(targets))
    done = _done(engine, mondays, today)
    weeks = []
    for start in mondays:
        note = notes.get(start)
        parsed = parse_title(note.get("name") if note else None)
        goals = []
        for row in targets.get(start, []):
            fam = plan_q.family(row.get("type"))
            goal = row.get("targets") or {}
            time_s = goal.get("time_target")
            goals.append(
                {
                    "sport": plan_q._SPORT.get(fam or "", row.get("type")),
                    "family": fam,
                    "title": row.get("name"),
                    "time_s": float(time_s) if isinstance(time_s, (int, float)) else None,
                    "done_s": round(done.get((start, fam), 0.0), 1) if start <= today else None,
                }
            )
        planned = [g["time_s"] for g in goals if g["time_s"]]
        weeks.append(
            {
                "start": str(start),
                "end": str(start + timedelta(days=6)),
                "title": note.get("name") if note else None,
                "text": note.get("description") if note else None,
                **parsed,
                "targets": goals,
                "time_s": round(sum(planned), 1) if planned else None,
                "done_s": round(sum(g["done_s"] or 0 for g in goals), 1)
                if start <= today and goals
                else None,
                "when": "current" if start == monday else "past" if start < monday else "future",
            }
        )
    phases: list[dict[str, Any]] = []
    for week in weeks:
        if phases and phases[-1]["phase"] == week["phase"]:
            phases[-1]["end"] = week["end"]
            phases[-1]["weeks"].append(week["number"])
        else:
            phases.append(
                {
                    "phase": week["phase"],
                    "start": week["start"],
                    "end": week["end"],
                    "weeks": [week["number"]],
                }
            )
    end = today + timedelta(days=UPCOMING_DAYS - 1)
    calendar = plan_q.plan(engine, Window(today, end), today, include_sync_status=False)
    saved = stored_priorities(engine)
    races = plan_q._merge_races(garmin_races, icu_plan.races(engine, today))
    return {
        "today": str(today),
        "plan_start": calendar["plan_start"],
        "plan_source": calendar["plan_source"],
        "races": [plan_q._event_row(race, today, saved) for race in races],
        "phases": phases,
        "weeks": weeks,
        "current_week": next((w["start"] for w in weeks if w["when"] == "current"), None),
        "upcoming": {
            "start": str(today),
            "end": str(end),
            "items": [_session(i) for i in calendar["items"] if i["kind"] in plan_q.TRAINING_KINDS],
            "not_in_plan": [_session(i) for i in calendar["not_in_plan"]],
        },
    }


def _session(item: dict[str, Any]) -> dict[str, Any]:
    extra = item.get("extra") or {}
    return {
        "id": f"{item['source']}:{item['source_id']}",
        "date": item["date"],
        "title": item.get("title"),
        "sport": item.get("sport"),
        "status": item.get("status"),
        "source": item.get("source"),
        "in_plan": item.get("in_plan", True),
        "duration_s": plan_q.planned_duration_s(item),
        "distance_m": plan_q.planned_distance_m(item),
        "description": extra.get("description"),
        "done_by": (item.get("done_by") or [None])[0],
    }


def _done(engine: Engine, mondays: list[date], today: date) -> dict[tuple[date, str | None], float]:
    if not mondays or mondays[0] > today:
        return {}
    last = min(mondays[-1] + timedelta(days=6), today)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.local_date,
                activity_unified.c.sport,
                activity_unified.c.moving_s,
                activity_unified.c.elapsed_s,
            ).where(
                activity_unified.c.local_date.between(mondays[0], last),
                activity_unified.c.kind.is_distinct_from("coaching"),
            )
        ).all()
    out: dict[tuple[date, str | None], float] = defaultdict(float)
    for day, sport, moving_s, elapsed_s in rows:
        out[(day - timedelta(days=day.weekday()), plan_q.family(sport))] += float(
            moving_s or elapsed_s or 0
        )
    return out
