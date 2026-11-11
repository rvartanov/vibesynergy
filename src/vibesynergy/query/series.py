"""Time series and descriptive statistics over the unified daily values (MCP.md §3.3).

Daily values come from ``daily_unified`` (one best-source value per day and metric);
``activity.*`` metrics are summed from ``activity_unified`` per day, and ``checkin.*`` and
``pain.max`` come from the diary. Every aggregated value comes with ``n``, the number of days
it is based on, so thin data is visible. ``exclude_tags`` leaves out days covered by a diary
note with one of those tags (e.g. sick days).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import journal
from vibesynergy.query.catalog import ACTIVITY_METRICS, COMPUTED, DIARY_METRICS, KNOWN
from vibesynergy.store.schema import activity_unified, checkin, daily_unified, injury_log

GRANULARITIES = ("day", "week", "month", "year")
AGGS = (
    "mean",
    "median",
    "sum",
    "min",
    "max",
    "count",
    "first",
    "last",
    "std",
    "p10",
    "p25",
    "p75",
    "p90",
)


def daily_values(
    engine: Engine,
    metrics: list[str],
    window: Window,
    *,
    sport: str | None = None,
    exclude_tags: Iterable[str] | None = None,
) -> dict[str, dict[date, float]]:
    """{metric: {date: value}} for stored, activity-derived and diary metrics, without the
    days tagged with any of ``exclude_tags``."""
    out: dict[str, dict[date, float]] = {m: {} for m in metrics}
    stored = [m for m in metrics if m not in COMPUTED]
    with engine.connect() as conn:
        if stored:
            rows = conn.execute(
                select(daily_unified.c.metric, daily_unified.c.date, daily_unified.c.value).where(
                    daily_unified.c.metric.in_(stored),
                    daily_unified.c.date.between(window.start, window.end),
                )
            )
            for metric, day, value in rows:
                out[metric][day] = value
        for metric in metrics:
            if metric not in ACTIVITY_METRICS:
                continue
            agg, column = ACTIVITY_METRICS[metric]
            value = func.count() if agg == "count" else func.sum(activity_column(column))
            query = (
                select(activity_unified.c.local_date, value)
                .where(activity_unified.c.local_date.between(window.start, window.end))
                .group_by(activity_unified.c.local_date)
            )
            if sport:
                query = query.where(activity_unified.c.sport.ilike(f"%{sport}%"))
            for day, v in conn.execute(query):
                if v is not None:
                    out[metric][day] = float(v)
        tables = {"checkin": checkin, "injury_log": injury_log}
        for metric in metrics:
            if metric not in DIARY_METRICS:
                continue
            table_name, column = DIARY_METRICS[metric]
            table = tables[table_name]
            col = table.c[column]
            value = func.max(col) if table_name == "injury_log" else func.min(col)
            query = (
                select(table.c.date, value)
                .where(table.c.date.between(window.start, window.end), col.is_not(None))
                .group_by(table.c.date)
            )
            for day, v in conn.execute(query):
                out[metric][day] = float(v)
    tags = [t for t in (exclude_tags or []) if t]
    if tags:
        skip = journal.tagged_days(engine, window, tags)
        if skip:
            out = {m: {d: v for d, v in vals.items() if d not in skip} for m, vals in out.items()}
    return out


def activity_column(name: str):
    """An activity_unified column; time is moving time, or elapsed time where the source
    gives none (Withings activities, many indoor sessions)."""
    t = activity_unified
    return func.coalesce(t.c.moving_s, t.c.elapsed_s) if name == "moving_s" else t.c[name]


def first_date(engine: Engine, metrics: list[str]) -> date | None:
    """The earliest date any of ``metrics`` has a value (to trim an open-ended period)."""
    daily = [m for m in metrics if m not in COMPUTED]
    candidates = []
    with engine.connect() as conn:
        if daily:
            candidates.append(
                conn.execute(
                    select(func.min(daily_unified.c.date)).where(daily_unified.c.metric.in_(daily))
                ).scalar()
            )
        if any(m in ACTIVITY_METRICS for m in metrics):
            candidates.append(
                conn.execute(select(func.min(activity_unified.c.local_date))).scalar()
            )
        if any(DIARY_METRICS.get(m, ("",))[0] == "checkin" for m in metrics):
            candidates.append(conn.execute(select(func.min(checkin.c.date))).scalar())
        if "pain.max" in metrics:
            candidates.append(conn.execute(select(func.min(injury_log.c.date))).scalar())
    found = [c for c in candidates if c is not None]
    return min(found) if found else None


def bucket(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    if granularity == "year":
        return day.replace(month=1, day=1)
    return day


def auto_granularity(window: Window) -> str:
    days = len(window)
    return "day" if days <= 180 else "week" if days <= 3 * 366 else "month"


def aggregate(values: list[float], agg: str) -> float | None:
    if not values:
        return None
    if agg == "mean":
        return statistics.fmean(values)
    if agg == "median":
        return statistics.median(values)
    if agg == "sum":
        return math.fsum(values)
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "count":
        return float(len(values))
    if agg == "first":
        return values[0]
    if agg == "last":
        return values[-1]
    if agg == "std":
        return statistics.stdev(values) if len(values) > 1 else 0.0
    if agg.startswith("p") and agg[1:].isdigit():
        return percentile(values, int(agg[1:]))
    raise ValueError(f"unknown agg {agg!r}; one of {', '.join(AGGS)}")


def percentile(values: Iterable[float], p: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def default_agg(metric: str) -> str:
    return KNOWN[metric].period_agg if metric in KNOWN else "mean"


def series(
    engine: Engine,
    metrics: list[str],
    window: Window,
    *,
    granularity: str | None = None,
    agg: dict[str, str] | None = None,
    sport: str | None = None,
    max_points: int = 400,
) -> dict[str, Any]:
    """Aligned series: ``columns`` = ["period", *metrics], ``rows``, per-row ``n``."""
    granularity = granularity or auto_granularity(window)
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    order = list(GRANULARITIES)
    while granularity != "year" and _buckets(window, granularity) > max_points:
        granularity = order[order.index(granularity) + 1]  # coarsen to fit max_points
    aggs = {m: (agg or {}).get(m, default_agg(m)) for m in metrics}
    values = daily_values(engine, metrics, window, sport=sport)
    grouped: dict[date, dict[str, list[float]]] = {}
    for metric, by_day in values.items():
        for day in sorted(by_day):
            grouped.setdefault(bucket(day, granularity), {}).setdefault(metric, []).append(
                by_day[day]
            )
    rows, counts = [], []
    for period in sorted(grouped):
        row: list[Any] = [str(period)]
        n = {}
        for metric in metrics:
            vals = grouped[period].get(metric, [])
            row.append(_round(aggregate(vals, aggs[metric])))
            n[metric] = len(vals)
        rows.append(row)
        counts.append(n)
    return {
        "columns": ["period", *metrics],
        "rows": rows,
        "n": counts,
        "granularity": granularity,
        "agg": aggs,
        "days_with_data": {m: len(values[m]) for m in metrics},
    }


def stats(
    engine: Engine,
    metrics: list[str],
    window: Window,
    *,
    exclude_tags: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    values = daily_values(engine, metrics, window, exclude_tags=exclude_tags)
    out = {}
    for metric in metrics:
        by_day = values[metric]
        vals = [by_day[d] for d in sorted(by_day)]
        if not vals:
            out[metric] = {"n": 0}
            continue
        out[metric] = {
            "n": len(vals),
            "mean": _round(statistics.fmean(vals)),
            "median": _round(statistics.median(vals)),
            "std": _round(statistics.stdev(vals)) if len(vals) > 1 else 0.0,
            "min": _round(min(vals)),
            "max": _round(max(vals)),
            **{f"p{p}": _round(percentile(vals, p)) for p in (10, 25, 75, 90)},
            "first": {"date": str(min(by_day)), "value": _round(vals[0])},
            "last": {"date": str(max(by_day)), "value": _round(vals[-1])},
            "change": _round(vals[-1] - vals[0]),
        }
    return out


def baseline(
    engine: Engine,
    metric: str,
    day: date,
    *,
    window_days: int = 60,
    exclude_tags: Iterable[str] | None = journal.BASELINE_EXCLUDE_TAGS,
) -> dict[str, Any] | None:
    """Personal norm over the ``window_days`` before ``day`` and where ``day`` falls in it.
    Days tagged in the diary with ``exclude_tags`` (default: sick) are left out of the
    norm, so a cold doesn't drag it down; the day itself is always compared."""
    before = Window(day - timedelta(days=window_days), day - timedelta(days=1))
    history = list(
        daily_values(engine, [metric], before, exclude_tags=exclude_tags)[metric].values()
    )
    today = daily_values(engine, [metric], Window(day, day))[metric].get(day)
    if len(history) < 7 or today is None:
        return None
    mean, std = statistics.fmean(history), statistics.stdev(history)
    return {
        "value": _round(today),
        "baseline_mean": _round(mean),
        "baseline_std": _round(std),
        "z": _round((today - mean) / std) if std else 0.0,
        "n": len(history),
    }


def _buckets(window: Window, granularity: str) -> int:
    return len({bucket(d, granularity) for d in window.days()})


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3) if abs(value) < 1000 else round(value, 1)
