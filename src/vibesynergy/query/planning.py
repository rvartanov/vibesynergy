"""Planning & progress: plan against what was done over the weeks, and where the
plan leads.

- ``plan_vs_done``: per Monday-to-Sunday week (the same ``week_progress`` the start page's
  week card uses), the goal per sport (the plan's weekly target time, else the planned
  workouts) against time done, the workouts done / changed / missed, and intervals.icu's
  compliance for the paired ones; plus the same per training phase (from the weekly notes).
- ``projection``: fitness, fatigue and form from today to the last upcoming race if the plan
  is done as written, run through VibeSynergy's model (``fitness_model.project``) from
  today's values. A session's planned load comes from its steps (``planned_load``); one
  without steps it can count (strength, Pilates, a free ride) gets its planned time at the
  athlete's own recent load per hour for that kind of session; a week with only a weekly
  target gets that target (its load, else its time at that rate) spread over its remaining
  days. It is a projection, labelled as one, with these assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import (
    fitness_model,
    plan_model,
    planned_load,
    training,
    training_plan,
    week_progress,
    workout_model,
)
from vibesynergy.query import plan as plan_q
from vibesynergy.store.schema import activity_unified

TREND_WEEKS = 12
MAX_TREND_WEEKS = 52
PROJECTION_MAX_DAYS = 300
RATE_DAYS = 42  # recent load per hour from this many days
FITNESS_RANGES = frozenset({42, 90, 182, 365})  # days of past in the fitness chart
DEFAULT_LOAD_PER_HOUR = 50.0  # when there is no recent history to learn from
ASSUMPTIONS = (
    "plan_done_as_written",
    "workout_load_from_steps",
    "missing_load_from_recent_load_per_hour",
    "weekly_targets_spread_evenly",
    "nothing_else_added",
)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


# Plan against done -----------------------------------------------------------------------


def plan_vs_done(
    engine: Engine,
    raw,
    today: date,
    *,
    first: date | None = None,
    last: date | None = None,
    weeks: int = TREND_WEEKS,
    overview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Weeks from ``first`` through ``last`` (Mondays; default the ``weeks`` weeks ending
    with this one), each against the plan, and the same per phase."""
    last = monday_of(last or today)
    first = monday_of(first) if first else last - timedelta(weeks=max(1, weeks) - 1)
    if first > last:
        first, last = last, first
    if (last - first).days // 7 + 1 > MAX_TREND_WEEKS:
        first = last - timedelta(weeks=MAX_TREND_WEEKS - 1)
    plan = plan_q.plan(
        engine, Window(first, last + timedelta(days=13)), today, include_sync_status=False
    )
    overview = overview if overview is not None else training_plan.overview(engine, today)
    phase_of = {w["start"]: w for w in overview.get("weeks") or []}
    rows = []
    monday = first
    while monday <= last:
        progress = week_progress.week_progress(engine, monday, today, plan=plan, raw=raw)
        rows.append(_week_row(progress, phase_of.get(str(monday)), today))
        monday += timedelta(days=7)
    return {
        "today": str(today),
        "start": str(first),
        "end": str(last + timedelta(days=6)),
        "weeks": rows,
        "phases": _phases(rows),
        "totals": _totals(rows),
    }


def _pct(part: float | None, whole: float | None) -> float | None:
    if not whole or part is None:
        return None
    return round(part / whole * 100, 1)


def _week_row(progress: dict[str, Any], plan_week: dict | None, today: date) -> dict[str, Any]:
    counts = progress["counts"]
    judged = counts["done"] + counts["changed"] + counts["missed"]
    # The column is intervals.icu's compliance: the time-based fallback (8-11) stays out.
    paired = [
        s["compliance"]
        for s in progress["sessions"]
        if s.get("compliance") and s.get("compliance_source") == "intervals"
    ]
    sports = []
    for sport in progress["sports"]:
        sports.append(
            {
                "family": sport["family"],
                "sport": sport["sport"],
                "goal_s": sport["goal_s"],
                "basis": sport["basis"],
                "target_s": sport["target_s"],
                "planned_s": sport["planned_s"],
                "done_s": sport["done_s"],
                "planned_load": sport["planned_load"],
                "done_load": sport["done_load"],
                "done_pct": _pct(sport["done_s"], sport["goal_s"]),
            }
        )
    goal = round(sum(s["goal_s"] or 0 for s in sports), 1) or None
    done = progress["totals"]["done_s"]
    start = date.fromisoformat(progress["start"])
    return {
        "start": progress["start"],
        "end": progress["end"],
        "when": "current"
        if start == monday_of(today)
        else "past"
        if start < monday_of(today)
        else "future",
        "number": (plan_week or {}).get("number"),
        "phase": (plan_week or {}).get("phase"),
        "kind": (plan_week or {}).get("kind"),
        "title": (progress.get("note") or {}).get("title"),
        "has_plan": progress["has_plan"],
        "goal_s": goal,
        "done_s": done,
        "remaining_s": progress["totals"]["remaining_s"],
        "done_pct": _pct(done, goal),
        "planned_load": round(sum(s["planned_load"] or 0 for s in sports), 1) or None,
        "done_load": round(sum(s["done_load"] or 0 for s in sports), 1) or None,
        "counts": counts,
        # Of the planned workouts whose day has passed: share done as planned.
        "sessions_done_pct": _pct(counts["done"], judged),
        "compliance_avg": round(sum(paired) / len(paired), 1) if paired else None,
        "sports": sports,
    }


def _phases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row["has_plan"]:
            continue
        if out and out[-1]["phase"] == row["phase"]:
            out[-1]["weeks"].append(row)
        else:
            out.append({"phase": row["phase"], "weeks": [row]})
    return [
        {"phase": p["phase"], "start": p["weeks"][0]["start"], "end": p["weeks"][-1]["end"]}
        | _totals(p["weeks"])
        for p in out
    ]


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    goal = round(sum(r["goal_s"] or 0 for r in rows), 1) or None
    done = round(sum(r["done_s"] or 0 for r in rows), 1)
    # The share of plan only compares weeks that had a plan; weeks without one would add
    # their whole training time against a goal of nothing.
    done_planned = round(sum(r["done_s"] or 0 for r in rows if r["goal_s"]), 1)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for key, value in row["counts"].items():
            counts[key] += value
    judged = counts["done"] + counts["changed"] + counts["missed"]
    paired = [r["compliance_avg"] for r in rows if r["compliance_avg"] is not None]
    return {
        "week_count": len(rows),
        "goal_s": goal,
        "done_s": done,
        "done_in_planned_weeks_s": done_planned,
        "done_pct": _pct(done_planned, goal),
        "counts": dict(counts),
        "sessions_done_pct": _pct(counts["done"], judged),
        "compliance_avg": round(sum(paired) / len(paired), 1) if paired else None,
    }


# Projection ----------------------------------------------------------------------------


def load_per_hour(engine: Engine, fitness_rows: list[list[Any]], today: date) -> float | None:
    """The athlete's recent load per hour of training, from the last ``RATE_DAYS`` days."""
    since = today - timedelta(days=RATE_DAYS - 1)
    load = sum(row[1] or 0 for row in fitness_rows if row[0] >= str(since))
    with engine.connect() as conn:
        seconds = conn.execute(
            select(
                func.sum(func.coalesce(activity_unified.c.moving_s, activity_unified.c.elapsed_s))
            ).where(activity_unified.c.local_date.between(since, today))
        ).scalar()
    hours = float(seconds or 0) / 3600
    if hours < 1 or load <= 0:
        return None
    return round(load / hours, 1)


def family_rates(
    engine: Engine, today: date, athlete: dict[str, Any] | None = None, *, parquet=None
) -> dict[str, float]:
    """The athlete's own load per hour per kind of session (ride, run, swim, strength, …)
    over the last ``RATE_DAYS`` days, for planned sessions without steps to count."""
    since = today - timedelta(days=RATE_DAYS - 1)
    load: dict[str, float] = defaultdict(float)
    hours: dict[str, float] = defaultdict(float)
    for item in training.activity_loads(engine, Window(since, today), athlete, parquet=parquet):
        load[item["family"]] += item["load"]
        hours[item["family"]] += item["seconds"] / 3600
    return {fam: round(load[fam] / hours[fam], 1) for fam in load if hours[fam] >= 1}


def projection(
    engine: Engine,
    raw,
    today: date,
    *,
    athlete: dict[str, Any] | None = None,
    overview: dict[str, Any] | None = None,
    fitness: dict[str, Any] | None = None,
    parquet=None,
) -> dict[str, Any]:
    """Fitness, fatigue and form per day from today to the last upcoming race (or the end of
    the plan) if the plan is done as written, with the projected values on each race day."""
    overview = overview if overview is not None else training_plan.overview(engine, today)
    fitness = fitness or training.training_load(
        engine,
        Window(today - timedelta(days=89), today),
        load="vibesynergy",
        athlete=athlete,
        raw=raw,
        parquet=parquet,
    )
    rows = fitness.get("rows") or []
    races = [r for r in overview.get("races") or [] if r.get("date") and r["date"] >= str(today)]
    plan_end = max((w["end"] for w in overview.get("weeks") or []), default=None)
    ends = [r["date"] for r in races] + ([plan_end] if plan_end else [])
    horizon = min(
        date.fromisoformat(max(ends)) if ends else today,
        today + timedelta(days=PROJECTION_MAX_DAYS),
    )
    base = {
        "label": "projection",
        "start": str(today),
        "end": str(horizon),
        "assumptions": list(ASSUMPTIONS),
        "fitness_source": (fitness.get("current") or {}).get("source"),
    }
    if not rows or horizon <= today:
        return {**base, "rows": [], "races": [], "basis": None}

    rate = load_per_hour(engine, rows, today)
    used_rate = rate or DEFAULT_LOAD_PER_HOUR
    inputs = training.ThresholdTimeline(engine, athlete).on(today)
    rates = family_rates(engine, today, athlete, parquet=parquet)
    plan = plan_q.plan(engine, Window(today, horizon), today, include_sync_status=False)
    # VibeSynergy's own plan holds each session's steps; an intervals.icu workout has them
    # in its text only.
    own = plan_model.leading_plan(engine)
    own_steps = plan_model.session_steps(engine, own, today, horizon) if own else {}
    daily: dict[date, float] = defaultdict(float)
    detail_days = from_time = 0
    detailed_weeks: set[date] = set()
    for item in plan.get("items") or []:
        if item["kind"] not in plan_q.TRAINING_KINDS:
            continue
        day = date.fromisoformat(item["date"])
        if day < today or item.get("status") in ("done", "changed", "missed"):
            continue
        extra = item.get("extra") or {}
        fam = plan_q.family(item.get("sport")) or "other"
        steps = own_steps.get(item.get("session_id"))
        if steps is None:
            steps = workout_model.parse_intervals(extra.get("description") or "").steps
        found = planned_load.planned_load(
            steps,
            fam,
            inputs,
            seconds=plan_q.planned_duration_s(item),
            rate_per_hour=rates.get(fam) or used_rate,
        )
        if found is None:
            continue
        load, how = found
        if how == "steps":
            detail_days += 1
        else:
            from_time += 1
        daily[day] += float(load)
        detailed_weeks.add(monday_of(day))
    target_weeks = 0
    for week in plan.get("weeks") or []:
        monday = date.fromisoformat(week["start"])
        if monday in detailed_weeks or monday + timedelta(days=6) <= today:
            continue
        targets = week.get("targets") or []
        load = sum(t["load"] for t in targets if t.get("load"))
        if not load:
            seconds = sum(t["time_s"] for t in targets if t.get("time_s"))
            load = seconds / 3600 * used_rate if seconds else 0
        if not load:
            continue
        days = [
            monday + timedelta(days=i)
            for i in range(7)
            if today < monday + timedelta(days=i) <= horizon
        ]
        for day in days:
            daily[day] += load / len(days)
        target_weeks += 1 if days else 0

    # Start from yesterday's state so today's still-open workouts count too.
    by_day = {row[0]: row for row in rows}
    yesterday = by_day.get(str(today - timedelta(days=1)))
    done_today = (by_day.get(str(today)) or [None, 0.0])[1] or 0.0
    loads = dict(daily)
    if yesterday:
        loads[today] = done_today + daily.get(today, 0.0)
        series = fitness_model.project(
            yesterday[2], yesterday[3], today - timedelta(days=1), loads, horizon
        )
    else:
        last = rows[-1]
        series = fitness_model.project(last[2], last[3], today, loads, horizon)
    by_date = {row["date"]: row for row in series}
    race_rows = []
    for race in races:
        found = by_date.get(race["date"])
        race_rows.append(
            {
                "title": race.get("title"),
                "date": race["date"],
                "days_away": race.get("days_away"),
                "priority": race.get("priority"),
                **(
                    {k: found[k] for k in ("ctl", "atl", "tsb", "form_pct", "form_zone")}
                    if found
                    else {}
                ),
            }
        )
    return {
        **base,
        "rows": series,
        "races": race_rows,
        "basis": {
            "workouts_with_load": detail_days,
            "workouts_from_time": from_time,
            "weeks_from_targets": target_weeks,
            "load_per_hour": used_rate,
            "load_per_hour_from": "recent" if rate else "default",
            "start_ctl": series[0]["ctl"] if series else None,
        },
    }


# The page ------------------------------------------------------------------------------


def page(
    engine: Engine,
    raw,
    today: date,
    *,
    week: date | None = None,
    athlete: dict[str, Any] | None = None,
    fitness_days: int = 90,
    parquet=None,
) -> dict[str, Any]:
    """Everything the planning & progress page shows, for the week of ``week``.
    ``fitness_days`` is how much past the fitness chart shows: 42, 90, 182 or 365."""
    if fitness_days not in FITNESS_RANGES:
        raise ValueError(f"fitness range must be one of {sorted(FITNESS_RANGES)} days")
    monday = monday_of(week or today)
    overview = training_plan.overview(engine, today)
    # The trend ends with the chosen week, or with this one when a future week is chosen.
    trend_last = min(monday, monday_of(today))
    return {
        "today": str(today),
        "week": week_progress.week_progress(engine, monday, today, raw=raw),
        "trend": plan_vs_done(engine, raw, today, last=trend_last, overview=overview),
        **form(
            engine,
            raw,
            today,
            athlete=athlete,
            fitness_days=fitness_days,
            overview=overview,
            parquet=parquet,
        ),
        "plan": overview,
    }


def form(
    engine: Engine,
    raw,
    today: date,
    *,
    athlete: dict[str, Any] | None = None,
    fitness_days: int = 90,
    overview: dict[str, Any] | None = None,
    parquet=None,
) -> dict[str, Any]:
    """Fitness, fatigue and form over ``fitness_days`` and the projection to the races:
    the Form tab under Fitness and the race cards on Planning & progress."""
    if fitness_days not in FITNESS_RANGES:
        raise ValueError(f"fitness range must be one of {sorted(FITNESS_RANGES)} days")
    overview = overview or training_plan.overview(engine, today)
    fitness = training.training_load(
        engine,
        Window(today - timedelta(days=fitness_days - 1), today),
        load="vibesynergy",
        athlete=athlete,
        raw=raw,
        parquet=parquet,
    )
    return {
        "fitness": {
            "columns": fitness["columns"],
            "rows": fitness["rows"],
            "current": fitness["current"],
            "source": fitness["source"],
            "days_by_source": fitness["days_by_source"],
        },
        "projection": projection(
            engine,
            raw,
            today,
            athlete=athlete,
            overview=overview,
            fitness=fitness,
            parquet=parquet,
        ),
    }
