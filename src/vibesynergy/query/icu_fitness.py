"""Fitness, fatigue and form the way intervals.icu computes them.

intervals.icu's fitness chart (checked against its forum, where the author explains it):

- Load per activity is intervals.icu's own ``icu_training_load``.
- Fitness (CTL) and fatigue (ATL) are exponentially weighted averages of the daily load
  with 42- and 7-day time constants: ``ctl += (load - ctl) * (1 - e^(-1/42))``.
- Form (TSB) is the same day's fitness minus fatigue (not yesterday's, as some tools do).
- Ramp rate is the change in fitness over 7 days.
- Form zones are Joe Friel's, read on form as a percentage of fitness: transition above
  +20 %, fresh +5 to +20 %, grey zone -10 to +5 %, optimal -30 to -10 %, high risk below
  -30 %.

Where VibeSynergy has them, intervals.icu's own numbers are used as they are: the wellness
record of a day (``ctl``, ``atl``, ``rampRate``, ``ctlLoad``) and each activity's
``icu_training_load``, both read from the raw archive. Only a day without an intervals.icu
wellness record is computed here, with the same algorithm, continuing from the last
intervals.icu value; its load is intervals.icu's activity load when intervals.icu has the
day's activities, else VibeSynergy's own TRIMP estimate. Each row says which (``source``,
``load_source``), so a page can label the estimate.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, and_, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import activity_effort
from vibesynergy.store.schema import activity_unified, raw_item

log = logging.getLogger(__name__)

ICU = "intervals_icu"
CTL_DAYS = 42
ATL_DAYS = 7
K_CTL = 1 - math.exp(-1 / CTL_DAYS)
K_ATL = 1 - math.exp(-1 / ATL_DAYS)
# Lower bound of each form zone in % of fitness, high to low (intervals.icu, after Friel).
FORM_ZONES = (
    (20.0, "transition"),
    (5.0, "fresh"),
    (-10.0, "grey"),
    (-30.0, "optimal"),
    (None, "high_risk"),
)
# intervals.icu activity types per VibeSynergy sport filter (the Training page's choices).
SPORT_TYPES = {
    "run": ("run",),
    "cycl": ("ride", "cycl"),
    "walk": ("walk", "hike"),
    "swim": ("swim",),
}
INTERVALS = "intervals"
COMPUTED = "computed"
TRIMP = "trimp"

_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
_CACHE_MAX = 64


# Form ----------------------------------------------------------------------------------


def form_pct(ctl: float | None, tsb: float | None) -> float | None:
    """Form as a percentage of fitness, as intervals.icu's chart reads its zones."""
    if ctl is None or tsb is None or ctl <= 0:
        return None
    return round(tsb / ctl * 100, 1)


def form_zone(ctl: float | None, tsb: float | None) -> str | None:
    pct = form_pct(ctl, tsb)
    if pct is None:
        return None
    for low, name in FORM_ZONES:
        if low is None or pct > low:
            return name
    return None  # pragma: no cover


def legacy_form(tsb: float | None) -> str | None:
    """The older absolute-TSB band other modules still read (safety rules, dashboard)."""
    if tsb is None:
        return None
    if tsb > 5:
        return "fresh"
    if tsb > -10:
        return "neutral"
    if tsb > -30:
        return "optimal training"
    return "overreaching risk"


def step(ctl: float, atl: float, load: float) -> tuple[float, float]:
    """One day of intervals.icu's fitness model."""
    return ctl + (load - ctl) * K_CTL, atl + (load - atl) * K_ATL


# Raw archive ---------------------------------------------------------------------------


def _months(window: Window) -> list[str]:
    out, day = [], window.start.replace(day=1)
    while day <= window.end:
        out.append(day.strftime("%Y-%m"))
        day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    # An activity late on the last day can sit in the next month's payload.
    out.append(day.strftime("%Y-%m"))
    return out


def _payloads(engine: Engine, raw, dataset: str, window: Window) -> list[list[dict]]:
    if raw is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.id, raw_item.c.sha256, raw_item.c.item_key).where(
                raw_item.c.source == ICU,
                raw_item.c.dataset == dataset,
                raw_item.c.is_current,
                raw_item.c.item_key.in_(_months(window)),
            )
        ).all()
    out = []
    for row in sorted(rows, key=lambda r: r.item_key):
        key = (dataset, row.sha256)
        found = _cache.get(key)
        if found is None:
            try:
                payload = raw.load(row.id).json()
            except Exception:
                log.warning("intervals.icu %s %s could not be read", dataset, row.item_key)
                payload = []
            found = (
                [item for item in payload if isinstance(item, dict)]
                if isinstance(payload, list)
                else []
            )
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = found
        out.append(found)
    return out


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def wellness_days(engine: Engine, raw, window: Window) -> dict[date, dict[str, float | None]]:
    """intervals.icu's wellness per day in the window: ``ctl``, ``atl``, ``ramp`` and
    ``load`` (its ``ctlLoad``). Only days with both fitness and fatigue."""
    out: dict[date, dict[str, float | None]] = {}
    for payload in _payloads(engine, raw, "wellness", window):
        for item in payload:
            day = _day(item.get("id"))
            ctl, atl = _number(item.get("ctl")), _number(item.get("atl"))
            if day is None or not window.start <= day <= window.end or ctl is None or atl is None:
                continue
            out[day] = {
                "ctl": ctl,
                "atl": atl,
                "ramp": _number(item.get("rampRate")),
                "load": _number(item.get("ctlLoad")),
            }
    return out


def icu_activities(engine: Engine, raw, window: Window) -> list[dict[str, Any]]:
    """intervals.icu activities in the window with their local date, type and load."""
    seen: set[str] = set()
    out = []
    for payload in _payloads(engine, raw, "activities", window):
        for item in payload:
            ident = str(item.get("id") or "")
            day = _day(item.get("start_date_local"))
            if not ident or ident in seen or day is None:
                continue
            if not window.start <= day <= window.end:
                continue
            seen.add(ident)
            out.append(
                {
                    "id": ident,
                    "date": day,
                    "type": str(item.get("type") or ""),
                    "load": _number(item.get("icu_training_load")),
                }
            )
    return out


def _matches(sport: str | None, icu_type: str) -> bool:
    if not sport:
        return True
    words = SPORT_TYPES.get(sport, (sport,))
    return any(word in icu_type.lower() for word in words)


def daily_icu_load(
    engine: Engine, raw, window: Window, *, sport: str | None = None
) -> dict[date, float]:
    """intervals.icu's load per day (sum of ``icu_training_load``), only days where it has
    at least one activity with a load."""
    out: dict[date, float] = defaultdict(float)
    for item in icu_activities(engine, raw, window):
        if item["load"] is not None and _matches(sport, item["type"]):
            out[item["date"]] += item["load"]
    return dict(out)


def coaching_icu_load(engine: Engine, raw, window: Window, *, sport: str | None = None):
    """intervals.icu's load of the trainings you give, per day (``kind`` coaching)."""
    t = activity_unified
    where = [t.c.local_date.between(window.start, window.end), t.c.kind == "coaching"]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    with engine.connect() as conn:
        groups = [
            dict(row)
            for row in conn.execute(
                select(t.c.group_id, t.c.sources, t.c.start_utc, t.c.local_date).where(and_(*where))
            ).mappings()
        ]
    for group in groups:
        group["sources"] = group.get("sources") or []
    if not groups or raw is None:
        return {}
    found = activity_effort.icu_for_groups(engine, raw, groups)
    out: dict[date, float] = defaultdict(float)
    for group in groups:
        load = (found.get(group["group_id"]) or {}).get("load")
        day = _day(group["local_date"])
        if load is not None and day is not None:
            out[day] += load
    return dict(out)


# The series ----------------------------------------------------------------------------


def fitness(
    engine: Engine,
    raw,
    window: Window,
    *,
    sport: str | None = None,
    include_coaching: bool = True,
    trimp_daily: dict[date, float] | None = None,
    trimp_coaching: dict[date, float] | None = None,
    warmup_days: int = 126,
) -> dict[str, Any]:
    """Daily fitness, fatigue, form, form %, ramp rate and load over ``window``.

    intervals.icu's wellness values win on every day it has them, but only for the whole
    picture (all sports, coaching included): its wellness has no per-sport or
    without-coaching split, so those views are computed from its activity loads.
    ``trimp_daily`` (and ``trimp_coaching``, the coaching part of it) is VibeSynergy's own
    load for days intervals.icu has no activities."""
    warmup = Window(window.start - timedelta(days=warmup_days), window.end)
    whole = sport is None and include_coaching
    wellness = wellness_days(engine, raw, warmup) if whole else {}
    icu_daily = daily_icu_load(engine, raw, warmup, sport=sport)
    coaching = coaching_icu_load(engine, raw, warmup, sport=sport) if raw is not None else {}
    trimp_daily = trimp_daily or {}
    trimp_coaching = trimp_coaching or {}

    ctl = atl = 0.0
    history: list[float] = []
    rows: list[list[Any]] = []
    coaching_daily: dict[str, float] = {}
    counts = {INTERVALS: 0, COMPUTED: 0}
    load_counts = {INTERVALS: 0, TRIMP: 0}
    for day in warmup.days():
        if day in icu_daily:
            day_load, load_source = icu_daily[day], INTERVALS
            day_coaching = coaching.get(day, 0.0)
        else:
            day_load = trimp_daily.get(day, 0.0)
            load_source = TRIMP if day_load else None
            day_coaching = trimp_coaching.get(day, 0.0)
        if not include_coaching:
            day_load = max(0.0, day_load - day_coaching)
        found = wellness.get(day)
        if found:
            ctl, atl, source = found["ctl"], found["atl"], INTERVALS
            if found["load"] is not None:
                day_load, load_source = found["load"], INTERVALS
        else:
            ctl, atl = step(ctl, atl, day_load)
            source = COMPUTED
        history.append(ctl)
        if day < window.start:
            continue
        ramp = found["ramp"] if found and found["ramp"] is not None else None
        if ramp is None and len(history) > 7:
            ramp = ctl - history[-8]
        tsb = ctl - atl
        rows.append(
            [
                str(day),
                round(day_load, 1),
                round(ctl, 1),
                round(atl, 1),
                round(tsb, 1),
                round(ramp, 1) if ramp is not None else None,
                form_pct(ctl, tsb),
                source,
                load_source,
            ]
        )
        counts[source] += 1
        if load_source:
            load_counts[load_source] += 1
        if include_coaching and day_coaching:
            coaching_daily[str(day)] = round(day_coaching, 1)
    last = rows[-1] if rows else None
    current = None
    if last:
        current = {
            "ctl": last[2],
            "atl": last[3],
            "tsb": last[4],
            "ctl_ramp_7d": last[5],
            "form_pct": last[6],
            "form_zone": form_zone(last[2], last[4]),
            "form": legacy_form(last[4]),
            "source": last[7],
        }
    return {
        "columns": [
            "date",
            "load",
            "ctl",
            "atl",
            "tsb",
            "ramp",
            "form_pct",
            "source",
            "load_source",
        ],
        "rows": rows,
        "current": current,
        "coaching_daily_load": coaching_daily,
        "days_by_source": counts,
        "load_days_by_source": load_counts,
        "has_intervals": bool(wellness or icu_daily),
    }


def project(
    ctl: float, atl: float, start: date, loads: dict[date, float], end: date
) -> list[dict[str, Any]]:
    """Fitness, fatigue and form from the day after ``start`` through ``end`` if the
    athlete does exactly ``loads`` (a planned load per day; days without one rest)."""
    out = []
    day = start + timedelta(days=1)
    while day <= end:
        ctl, atl = step(ctl, atl, loads.get(day, 0.0))
        tsb = ctl - atl
        out.append(
            {
                "date": str(day),
                "load": round(loads.get(day, 0.0), 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(tsb, 1),
                "form_pct": form_pct(ctl, tsb),
                "form_zone": form_zone(ctl, tsb),
            }
        )
        day += timedelta(days=1)
    return out
