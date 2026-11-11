"""Blocks of the start page that the dashboard reply doesn't carry: body and
measurements with the weight trend, what is coming up in the next days, and the best running
efforts over a period the athlete chooses. The portal reads these through its API; the
numbers are the same ones the MCP tools give (``vibesynergy_body``, ``vibesynergy_plan``,
``vibesynergy_bests``).
"""

from __future__ import annotations

from datetime import date, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import catalog, periods, training
from vibesynergy.query import plan as plan_q
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import activity_unified, daily_unified

BODY_DAYS = 90  # how far back the weight trend and "latest" values reach
WEIGHT_STABLE_KG = 0.5  # a change smaller than this over 30 days reads as "stable"
COMPOSITION = (
    "body.fat_pct",
    "body.muscle_mass",
    "body.fat_mass",
    "body.lean_mass",
    "body.water_mass",
    "body.bone_mass",
)
BLOOD_PRESSURE = ("bp.systolic", "bp.diastolic", "bp.pulse")

UPCOMING_DAYS = 7

# The periods the start page offers for best running efforts; the first is the default.
BEST_RUN_PERIODS = ("last_90d", "last_365d", "this_year", "all")


# Body and measurements -----------------------------------------------------------------------


def body(engine: Engine, when: date) -> dict[str, Any]:
    """Weight with its trend, the latest body composition and blood pressure, each with the
    date it was measured, up to ``BODY_DAYS`` before ``when``."""
    metrics = ("body.weight", *COMPOSITION, *BLOOD_PRESSURE)
    # Twice the span, so the 90-day change has a reading to compare with.
    first = when - timedelta(days=2 * BODY_DAYS)
    with engine.connect() as conn:
        rows = conn.execute(
            select(daily_unified.c.metric, daily_unified.c.date, daily_unified.c.value)
            .where(
                daily_unified.c.metric.in_(metrics),
                daily_unified.c.date.between(first, when),
            )
            .order_by(daily_unified.c.date)
        ).all()
    by_metric: dict[str, list[tuple[date, float]]] = {}
    for metric, day, value in rows:
        if value is not None:
            by_metric.setdefault(metric, []).append((day, float(value)))
    recent = when - timedelta(days=BODY_DAYS - 1)

    def latest(metric: str) -> dict[str, Any] | None:
        points = [p for p in by_metric.get(metric, []) if p[0] >= recent]
        if not points:
            return None
        day, value = points[-1]
        return {"metric": metric, "value": value, "unit": _unit(metric), "date": str(day)}

    composition = [row for m in COMPOSITION if (row := latest(m))]
    pressure = {m.split(".", 1)[1]: row for m in BLOOD_PRESSURE if (row := latest(m))}
    return {
        "date": str(when),
        "days": BODY_DAYS,
        "weight": _weight(by_metric.get("body.weight", []), recent),
        "composition": composition,
        "blood_pressure": pressure or None,
    }


def _weight(points: list[tuple[date, float]], recent: date) -> dict[str, Any] | None:
    shown = [(d, v) for d, v in points if d >= recent]
    if not shown:
        return None
    day, value = shown[-1]

    def change(days: int) -> float | None:
        before = [v for d, v in points if d <= day - timedelta(days=days)]
        return round(value - before[-1], 1) if before else None

    month = change(30)
    trend = None
    if month is not None:
        trend = "stable" if abs(month) < WEIGHT_STABLE_KG else "down" if month < 0 else "up"
    return {
        "value": value,
        "unit": "kg",
        "date": str(day),
        "change_30d": month,
        "change_90d": change(90),
        "trend": trend,
        "points": [[str(d), v] for d, v in shown],
    }


def _unit(metric: str) -> str:
    known = catalog.KNOWN.get(metric)
    return known.unit if known else ""


# Coming up -------------------------------------------------------------------------------------


def upcoming(engine: Engine, today: date, days: int = UPCOMING_DAYS) -> dict[str, Any]:
    """Planned workouts and races from today through the next ``days - 1`` days, per day.
    Personal calendar appointments stay out: the start page shows training, not the diary."""
    window = Window(today, today + timedelta(days=days - 1))
    reply = plan_q.plan(engine, window, today, include_calendar=False, include_sync_status=False)
    by_day: dict[str, list[dict[str, Any]]] = {str(d): [] for d in window.days()}
    for item in reply["items"]:
        kind = item["kind"]
        if kind in plan_q.TRAINING_KINDS:
            shown = "workout"
        elif kind == "event":
            shown = "race" if item.get("is_race") else "event"
        else:
            continue
        day = str(item["date"])
        if day not in by_day:  # a multi-day event that started earlier
            day = str(today)
        done = item.get("done_by") or []
        by_day[day].append(
            {
                "kind": shown,
                "title": item.get("title"),
                "sport": item.get("sport"),
                "status": item.get("status"),
                "activity_id": done[0]["id"] if done else None,
            }
        )
    return {
        "start": str(window.start),
        "end": str(window.end),
        "days": [{"date": d, "items": items} for d, items in by_day.items()],
    }


# Best running efforts --------------------------------------------------------------------------

_bests_cache: dict[tuple, dict[str, Any]] = {}
_bests_lock = Lock()


def best_runs_window(engine: Engine, period: str, today: date) -> Window:
    if period not in BEST_RUN_PERIODS:
        raise ValueError(f"period: one of {', '.join(BEST_RUN_PERIODS)}")
    window = periods.resolve(period, today=today)
    if period == "all":
        with engine.connect() as conn:
            first = conn.execute(
                select(func.min(activity_unified.c.local_date)).where(
                    activity_unified.c.stream_key.is_not(None)
                )
            ).scalar()
        window = Window(first or today, today)
    return window


def best_runs(engine: Engine, parquet: ParquetStore, period: str, today: date) -> dict[str, Any]:
    """Fastest running times (400 m to the half marathon) within one of BEST_RUN_PERIODS,
    with the dates the period covers. Reading every run's stream is slow over all time, so
    the answer is kept until an activity in the period is added or changes."""
    window = best_runs_window(engine, period, today)
    t = activity_unified
    with engine.connect() as conn:
        count, changed = conn.execute(
            select(func.count(), func.max(t.c.updated_at)).where(
                t.c.local_date.between(window.start, window.end), t.c.stream_key.is_not(None)
            )
        ).one()
    key = (str(parquet.root), window.start, window.end, count, str(changed))
    with _bests_lock:
        cached = _bests_cache.get(key)
    if cached is None:
        cached = training.pace_curve(engine, parquet, window)
        with _bests_lock:
            if len(_bests_cache) > 32:
                _bests_cache.clear()
            _bests_cache[key] = cached
    return {
        **cached,
        "period": period,
        "periods": list(BEST_RUN_PERIODS),
        "start": str(window.start),
        "end": str(window.end),
    }


BEST_SPORTS = ("run", "bike", "swim", "other", "all")


def bests(
    engine: Engine, parquet: ParquetStore, sport: str, period: str, today: date
) -> dict[str, Any]:
    """Best efforts for one sport, or all sports together, within one of BEST_RUN_PERIODS: the same
    numbers as ``vibesynergy_bests`` (overview), cached like ``best_runs``."""
    from vibesynergy.query import bests as bests_q

    if sport not in BEST_SPORTS:
        raise ValueError(f"sport: one of {', '.join(BEST_SPORTS)}")
    window = best_runs_window(engine, period, today)
    t = activity_unified
    with engine.connect() as conn:
        count, changed = conn.execute(
            select(func.count(), func.max(t.c.updated_at)).where(
                t.c.local_date.between(window.start, window.end)
            )
        ).one()
    key = ("bests", sport, str(parquet.root), window.start, window.end, count, str(changed))
    with _bests_lock:
        cached = _bests_cache.get(key)
    if cached is None:
        cached = bests_q.overview(engine, parquet, window, sport=sport)
        with _bests_lock:
            if len(_bests_cache) > 32:
                _bests_cache.clear()
            _bests_cache[key] = cached
    return {
        **cached,
        "period": period,
        "periods": list(BEST_RUN_PERIODS),
        "sports": list(BEST_SPORTS),
        "start": str(window.start),
        "end": str(window.end),
    }
