"""Zones and thresholds per sport: the plan's values, their zones and what each platform holds.

The athlete's confirmed values live in ``athlete_threshold`` (status ``current``); that is
"the plan" and what the coach and workout generation use. This module adds what a person
needs around it: the derived zones (Friel heart rate, Coggan power, threshold-pace and CSS
pace zones), the values Garmin and intervals.icu currently hold with mismatches flagged,
measured references (resting HR, HRV, VO2max, highest heart rate) and a history per metric.

Writes go through ``athlete_threshold`` (propose → confirm), so every change keeps its row,
method, date and the test or race activity it came from. Nothing here writes to another
platform; that is ``threshold_push``.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, and_, desc, func, select, update

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.query import athlete_threshold
from vibesynergy.store.schema import (
    activity_unified,
    audit_log,
    coach_decision_log,
    daily_unified,
    zone_set,
)
from vibesynergy.store.schema import athlete_threshold as threshold_table

log = logging.getLogger(__name__)

SPORTS = ("bike", "run", "swim")
# Metrics shown (and editable) per sport, in display order, with their stored unit.
SPORT_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "bike": (("ftp", "W"), ("lthr", "bpm"), ("max_hr", "bpm")),
    "run": (
        ("lthr", "bpm"),
        ("max_hr", "bpm"),
        ("threshold_pace", "s/km"),
        ("threshold_power", "W"),
    ),
    "swim": (("css", "s/100m"), ("lthr", "bpm"), ("max_hr", "bpm")),
}
GENERAL_METRICS: tuple[tuple[str, str], ...] = (
    ("resting_hr_baseline", "bpm"),
    ("hrv_baseline", "ms"),
)
# Older rows use other names or units; they are read as the canonical metric.
SPORT_ALIASES = {"cycling": "bike", "ride": "bike", "running": "run", "swimming": "swim"}
RANGES: dict[str, tuple[float, float]] = {
    "ftp": (50, 600),
    "threshold_power": (50, 800),
    "lthr": (80, 220),
    "max_hr": (100, 230),
    "threshold_pace": (120, 900),  # s/km: 2:00-15:00 /km
    "css": (50, 300),  # s/100 m: 0:50-5:00 /100 m
    "resting_hr_baseline": (25, 120),
    "hrv_baseline": (5, 250),
    "vo2max": (20, 95),
}

# Friel heart-rate zones as upper bounds in % of LTHR: Z1, Z2, Z3, Z4, Z5a, Z5b; Z5c runs
# to max HR. Cycling and running use different tables (Friel, Training Bible).
FRIEL_HR = {
    "bike": (0.81, 0.89, 0.93, 0.99, 1.02, 1.06),
    "run": (0.85, 0.89, 0.94, 0.99, 1.02, 1.06),
}
FRIEL_LABELS = ("1", "2", "3", "4", "5a", "5b", "5c")
# Power zones as upper bounds in % of FTP (Coggan; also intervals.icu's default).
POWER_PCT = (0.55, 0.75, 0.90, 1.05, 1.20, 1.50)
# Friel run pace zones: zone edges as % of threshold-pace time (slow edge first).
RUN_PACE_PCT = (1.29, 1.14, 1.06, 1.01, 0.97, 0.90)
# Swim zones from CSS as % of CSS speed (intervals.icu's swim default).
SWIM_PACE_PCT = (0.775, 0.877, 0.943, 1.0, 1.034, 1.115)

GARMIN_SPORT = {"bike": "cycling", "run": "running", "swim": "swimming"}
ICU_TYPE = {"bike": "Ride", "run": "Run", "swim": "Swim"}
ACTIVITY_SPORTS = {
    "bike": ("cycling", "indoor_cycling", "virtual_ride", "road_biking", "gravel_cycling"),
    "run": ("running", "trail_running", "treadmill_running", "virtual_run", "track_running"),
    "swim": ("lap_swimming", "open_water_swimming", "swimming"),
}

IcuSettings = Callable[[], list[dict[str, Any]]]


def canonical(sport: str, metric: str, unit: str | None = None) -> tuple[str, str]:
    """(sport, metric) as this module names them; swim threshold pace is CSS."""
    sport_key = SPORT_ALIASES.get(sport.strip().lower(), sport.strip().lower())
    metric_key = metric.strip().lower()
    if sport_key == "swim" and metric_key == "threshold_pace":
        metric_key = "css"
    return sport_key, metric_key


def unit_for(sport: str, metric: str) -> str:
    for key, unit in SPORT_METRICS.get(sport, ()) + GENERAL_METRICS:
        if key == metric:
            return unit
    if metric == "vo2max":
        return "ml/kg/min"
    raise ValueError(f"{metric} is not a threshold for {sport}")


def stored_value(row: dict[str, Any]) -> float:
    """A row's value in the canonical unit (old run pace rows are min/km)."""
    value = float(row["value"])
    if row.get("unit") == "min/km":
        return round(value * 60, 1)
    return value


def to_canonical(sport: str, metric: str, value: float, unit: str | None) -> float:
    """Convert a value given in another common unit (m/s, min/km, min/100m) to ours."""
    given = (unit or "").strip().lower().replace(" ", "")
    if metric in {"threshold_pace", "css"}:
        distance = 1000 if metric == "threshold_pace" else 100
        if given in {"m/s", "mps"}:
            return round(distance / float(value), 1)
        if given in {"min/km", "min/100m"}:
            return round(float(value) * 60, 1)
    return float(value)


def validate(sport: str, metric: str, value: float) -> None:
    if sport not in SPORT_METRICS and sport != "general":
        raise ValueError(f"sport must be one of {', '.join((*SPORTS, 'general'))}")
    unit_for(sport, metric)
    low, high = RANGES.get(metric, (0, math.inf))
    if not (isinstance(value, int | float) and math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{metric} must be between {low:g} and {high:g}")


# Zones ------------------------------------------------------------------------------------


def hr_zones(sport: str, lthr: float | None, max_hr: float | None) -> list[dict[str, Any]]:
    """Friel HR zones (bpm) from LTHR; the top zone ends at max HR when known."""
    pct = FRIEL_HR.get(sport)
    if not pct or not lthr:
        return []
    uppers = [math.floor(p * lthr) for p in pct]
    zones = []
    low: int | None = None
    for i, label in enumerate(FRIEL_LABELS):
        high = uppers[i] if i < len(uppers) else (round(max_hr) if max_hr else None)
        zones.append({"zone": i + 1, "label": label, "low": low, "high": high})
        low = (high + 1) if high is not None else None
    return zones


def hr_upper_bounds(zones: list[dict[str, Any]]) -> list[int]:
    """intervals.icu's ``hr_zones``: the top of each zone, ending with max HR."""
    return [z["high"] for z in zones if z["high"] is not None]


def garmin_hr_floors(zones: list[dict[str, Any]]) -> list[int]:
    """Garmin's five zone floors (zones 2-5) from the Friel zones: Friel Z5a-c are Garmin Z5."""
    return [z["low"] for z in zones[1:5] if z["low"] is not None]


def power_zones(ftp: float | None) -> list[dict[str, Any]]:
    if not ftp:
        return []
    uppers = [round(p * ftp) for p in POWER_PCT]
    zones, low = [], 0
    for i in range(7):
        high = uppers[i] if i < len(uppers) else None
        zones.append({"zone": i + 1, "label": str(i + 1), "low": low, "high": high})
        low = (high + 1) if high is not None else None
    return zones


def garmin_power_floors(ftp: float) -> list[int]:
    """Garmin's seven power-zone floors (zone 1 starts at 0)."""
    return [0, *(round(p * ftp) for p in POWER_PCT)]


def pace_zones(sport: str, threshold: float | None) -> list[dict[str, Any]]:
    """Pace zones in seconds per km (run) or per 100 m (swim): ``slow`` and ``fast`` edges."""
    if not threshold:
        return []
    if sport == "run":
        edges = [threshold * p for p in RUN_PACE_PCT]
        labels = FRIEL_LABELS
    elif sport == "swim":
        edges = [threshold / p for p in SWIM_PACE_PCT]
        labels = ("1", "2", "3", "4", "5", "6", "7")
    else:
        return []
    zones = []
    slow: float | None = None
    for i, label in enumerate(labels):
        fast = round(edges[i]) if i < len(edges) else None
        zones.append({"zone": i + 1, "label": label, "slow": slow, "fast": fast})
        slow = fast
    return zones


def derived_zones(sport: str, values: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    hr = hr_zones(sport, values.get("lthr"), values.get("max_hr"))
    if hr:
        out["hr"] = {"model": "friel", "zones": hr}
    if sport == "bike" and values.get("ftp"):
        out["power"] = {"model": "coggan", "zones": power_zones(values["ftp"])}
    pace_key = "css" if sport == "swim" else "threshold_pace"
    pace = pace_zones(sport, values.get(pace_key))
    if pace:
        out["pace"] = {"model": "css" if sport == "swim" else "friel", "zones": pace}
    return out


# Ledger reads -----------------------------------------------------------------------------


def _row(row: dict[str, Any]) -> dict[str, Any]:
    sport, metric = canonical(row["sport"], row["metric"])
    first = (row.get("history") or [{}])[0] if row.get("history") else {}
    activity = None
    for event in row.get("history") or []:
        if isinstance(event, dict) and event.get("activity_id"):
            activity = event["activity_id"]
            break
    return {
        "id": row["id"],
        "sport": sport,
        "metric": metric,
        "value": stored_value(row),
        "unit": "s/km" if row.get("unit") == "min/km" else row["unit"],
        "method": row["method"],
        "date_set": row["date_set"].isoformat()
        if isinstance(row.get("date_set"), date)
        else row.get("date_set"),
        "status": row["status"],
        "activity_id": activity,
        "note": first.get("note") if isinstance(first, dict) else None,
        "created_at": row["created_at"].isoformat()
        if hasattr(row.get("created_at"), "isoformat")
        else row.get("created_at"),
        "superseded_by": row.get("superseded_by"),
    }


def _ledger(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(threshold_table).order_by(threshold_table.c.id)).mappings()
        return [_row(dict(r)) for r in rows]


def current_values(engine: Engine) -> dict[str, dict[str, dict[str, Any]]]:
    """sport -> metric -> confirmed row (canonical names and units)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in _ledger(engine):
        if row["status"] != "current":
            continue
        have = out.setdefault(row["sport"], {}).get(row["metric"])
        if have is None or row["id"] > have["id"]:
            out[row["sport"]][row["metric"]] = row
    return out


def _proposals(engine: Engine, ledger: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Open proposals by ledger id, with the coach's message when there is one."""
    pending = {row["id"]: dict(row) for row in ledger if row["status"] == "proposed"}
    if not pending:
        return {}
    with engine.connect() as conn:
        entries = conn.execute(
            select(
                coach_decision_log.c.id,
                coach_decision_log.c.text,
                coach_decision_log.c.meta,
                coach_decision_log.c.at,
            ).where(coach_decision_log.c.entry_type == "threshold_change")
        ).mappings()
        for entry in entries:
            meta = entry["meta"] or {}
            row = pending.get(meta.get("athlete_threshold_id"))
            if row is not None:
                row["entry_id"] = entry["id"]
                row["summary"] = entry["text"]
                if not row.get("activity_id") and meta.get("activity_id"):
                    row["activity_id"] = meta["activity_id"]
    return pending


# Platforms --------------------------------------------------------------------------------


def garmin_values(engine: Engine) -> dict[str, dict[str, Any]]:
    """What Garmin Connect holds per sport, from the latest synced zone settings."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(zone_set)
            .where(zone_set.c.source == "garmin")
            .order_by(desc(zone_set.c.valid_from))
        ).mappings()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            latest.setdefault((row["kind"], row["sport"]), dict(row))
    out: dict[str, dict[str, Any]] = {}
    for sport, garmin_sport in GARMIN_SPORT.items():
        values: dict[str, Any] = {}
        hr = latest.get(("hr", garmin_sport)) or latest.get(("hr", "default"))
        if hr:
            values["lthr"] = hr["threshold"]
            values["max_hr"] = hr["max_value"]
            floors = list(hr["boundaries"] or [])
            if hr["max_value"] and floors and floors[-1] == hr["max_value"]:
                floors = floors[:-1]
            values["hr_floors"] = [round(v) for v in floors[1:5]]
            values["hr_profile"] = hr["sport"]
            values["as_of"] = hr["valid_from"].isoformat()
        power = latest.get(("power", garmin_sport))
        if power and power["threshold"]:
            key = "ftp" if sport == "bike" else "threshold_power"
            values[key] = power["threshold"]
            values.setdefault("as_of", power["valid_from"].isoformat())
        lactate = latest.get(("lt", "running")) if sport == "run" else None
        if lactate:
            extra = lactate["extra"] or {}
            values["profile_lt"] = {
                "lthr": lactate["threshold"],
                "pace_s_per_km": extra.get("pace_s_per_km"),
                "auto_detect": bool(extra.get("auto_detect")),
            }
        general = latest.get(("hr", "default"))
        if sport == "run" and general and hr is not general:
            # Garmin's general profile must equal RUNNING.
            floors = list(general["boundaries"] or [])
            if general["max_value"] and floors and floors[-1] == general["max_value"]:
                floors = floors[:-1]
            values["default_hr"] = {
                "lthr": general["threshold"],
                "max_hr": general["max_value"],
                "hr_floors": [round(v) for v in floors[1:5]],
            }
        css = latest.get(("css", garmin_sport))
        if css and css["threshold"]:
            values["css"] = round(100_000 / float(css["threshold"]), 1)  # mm/s → s/100 m
            values.setdefault("as_of", css["valid_from"].isoformat())
        if values:
            out[sport] = values
    return out


_ICU_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_ICU_LOCK = threading.Lock()
ICU_CACHE_SECONDS = 120


def icu_settings_live() -> list[dict[str, Any]]:
    """intervals.icu sport settings, cached two minutes (read on every page view)."""
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    with _ICU_LOCK:
        if (
            _ICU_CACHE["value"] is not None
            and time.monotonic() - _ICU_CACHE["at"] < ICU_CACHE_SECONDS
        ):
            return _ICU_CACHE["value"]
    value = IntervalsIcuClient.from_env().sport_settings()
    with _ICU_LOCK:
        _ICU_CACHE.update(at=time.monotonic(), value=value)
    return value


def icu_connected() -> bool:
    """Is intervals.icu set up (a key under Settings → Sources, or the legacy environment)?"""
    from vibesynergy.sources.intervals_icu import credentials

    try:
        return credentials.load() is not None
    except Exception:  # an unreadable store: let the read itself report it
        return True


def forget_icu_cache() -> None:
    with _ICU_LOCK:
        _ICU_CACHE.update(at=0.0, value=None)


def icu_settings_for(settings: list[dict[str, Any]], sport: str) -> dict[str, Any] | None:
    wanted = ICU_TYPE[sport]
    for item in settings:
        if wanted in (item.get("types") or []):
            return item
    return None


def icu_values(settings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sport in SPORTS:
        item = icu_settings_for(settings, sport)
        if not item:
            continue
        values: dict[str, Any] = {"settings_id": item.get("id")}
        if item.get("lthr"):
            values["lthr"] = item["lthr"]
        if item.get("max_hr"):
            values["max_hr"] = item["max_hr"]
        if item.get("hr_zones"):
            values["hr_zones"] = list(item["hr_zones"])
        if item.get("ftp"):
            values["ftp" if sport == "bike" else "threshold_power"] = item["ftp"]
        speed = item.get("threshold_pace")
        if speed:
            if sport == "swim":
                values["css"] = round(100 / float(speed), 1)
            elif sport == "run":
                values["threshold_pace"] = round(1000 / float(speed), 1)
        if item.get("updated"):
            values["as_of"] = str(item["updated"])[:10]
        out[sport] = values
    return out


def _same(metric: str, a: float, b: float) -> bool:
    tolerance = 1.0 if metric in {"threshold_pace", "css"} else 0.5
    return abs(float(a) - float(b)) <= tolerance


def mismatches(
    sport: str,
    plan: dict[str, float],
    zones: dict[str, Any],
    platforms: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Where a platform holds a different value than the plan (only metrics the plan sets)."""
    out = []
    for platform, values in platforms.items():
        if not values:
            continue
        for metric, wanted in plan.items():
            have = values.get(metric)
            if have is None or wanted is None:
                continue
            if not _same(metric, have, wanted):
                out.append(
                    {
                        "platform": platform,
                        "metric": metric,
                        "platform_value": have,
                        "plan_value": wanted,
                    }
                )
        hr = (zones.get("hr") or {}).get("zones") or []
        if platform == "intervals" and hr and values.get("hr_zones"):
            want = hr_upper_bounds(hr)
            if [round(v) for v in values["hr_zones"]] != want:
                out.append(
                    {
                        "platform": platform,
                        "metric": "hr_zones",
                        "platform_value": values["hr_zones"],
                        "plan_value": want,
                    }
                )
        general = values.get("default_hr") if platform == "garmin" else None
        if general:
            running = {k: values.get(k) for k in ("lthr", "max_hr", "hr_floors")}
            if general != running:
                out.append(
                    {
                        "platform": platform,
                        "metric": "hr_default",
                        "platform_value": general,
                        "plan_value": running,
                    }
                )
        profile = values.get("profile_lt") if platform == "garmin" else None
        if profile:
            lthr, pace = plan.get("lthr"), plan.get("threshold_pace")
            if lthr and profile["lthr"] and round(profile["lthr"]) != round(lthr):
                out.append(
                    {
                        "platform": platform,
                        "metric": "profile_lthr",
                        "platform_value": profile["lthr"],
                        "plan_value": lthr,
                    }
                )
            if pace and profile["pace_s_per_km"] and abs(profile["pace_s_per_km"] - pace) > 2:
                out.append(
                    {
                        "platform": platform,
                        "metric": "profile_pace",
                        "platform_value": profile["pace_s_per_km"],
                        "plan_value": pace,
                    }
                )
            if profile["auto_detect"]:
                out.append(
                    {
                        "platform": platform,
                        "metric": "auto_detect",
                        "platform_value": True,
                        "plan_value": False,
                    }
                )
        if platform == "garmin" and hr and values.get("hr_floors"):
            want = garmin_hr_floors(hr)
            if len(want) == 4 and values["hr_floors"] != want:
                out.append(
                    {
                        "platform": platform,
                        "metric": "hr_zones",
                        "platform_value": values["hr_floors"],
                        "plan_value": want,
                    }
                )
    return out


# Measured references ----------------------------------------------------------------------


def _latest_daily(engine: Engine, metric: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(daily_unified.c.date, daily_unified.c.value, daily_unified.c.source)
                .where(daily_unified.c.metric == metric)
                .order_by(desc(daily_unified.c.date))
                .limit(1)
            )
            .mappings()
            .first()
        )
    if not row:
        return None
    return {"value": row["value"], "date": row["date"].isoformat(), "source": row["source"]}


def _mean_daily(engine: Engine, metric: str, today: date, days: int) -> dict[str, Any] | None:
    from vibesynergy.query import series

    window = Window(today - timedelta(days=days), today)
    values = list(series.daily_values(engine, [metric], window)[metric].values())
    if len(values) < 7:
        return None
    return {"value": round(statistics.fmean(values), 1), "days": len(values)}


def _highest_hr(engine: Engine, sport: str, today: date) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    activity_unified.c.max_hr,
                    activity_unified.c.local_date,
                    activity_unified.c.group_id,
                    activity_unified.c.name,
                )
                .where(
                    activity_unified.c.sport.in_(ACTIVITY_SPORTS[sport]),
                    activity_unified.c.local_date >= today - timedelta(days=365),
                    activity_unified.c.max_hr.is_not(None),
                    activity_unified.c.max_hr <= 220,
                )
                .order_by(desc(activity_unified.c.max_hr))
                .limit(1)
            )
            .mappings()
            .first()
        )
    if not row:
        return None
    return {
        "value": row["max_hr"],
        "date": row["local_date"].isoformat(),
        "activity_id": row["group_id"],
        "name": row["name"],
    }


def _volume(engine: Engine, today: date) -> dict[str, float]:
    """Training time per sport over the last year, in seconds."""
    sport_of = {name: sport for sport, names in ACTIVITY_SPORTS.items() for name in names}
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.sport,
                func.sum(func.coalesce(activity_unified.c.moving_s, activity_unified.c.elapsed_s)),
            )
            .where(
                activity_unified.c.sport.in_(list(sport_of)),
                activity_unified.c.local_date >= today - timedelta(days=365),
            )
            .group_by(activity_unified.c.sport)
        ).all()
    out: dict[str, float] = {}
    for name, seconds in rows:
        out[sport_of[name]] = out.get(sport_of[name], 0.0) + float(seconds or 0.0)
    return out


def measured(engine: Engine, today: date) -> dict[str, Any]:
    return {
        "resting_hr": _mean_daily(engine, "hr.resting", today, 60),
        "resting_hr_latest": _latest_daily(engine, "hr.resting"),
        "hrv_low": _latest_daily(engine, "hrv.baseline_low"),
        "hrv_high": _latest_daily(engine, "hrv.baseline_high"),
        "hrv_week": _latest_daily(engine, "hrv.weekly_avg"),
        "vo2max_run": _latest_daily(engine, "fitness.vo2max.run"),
        "vo2max_bike": _latest_daily(engine, "fitness.vo2max.ride"),
    }


# Overview ---------------------------------------------------------------------------------


def _pushes(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_log.c.at, audit_log.c.action, audit_log.c.detail, audit_log.c.outcome)
            .where(audit_log.c.action.like("threshold.push.%"))
            .order_by(desc(audit_log.c.id))
            .limit(200)
        ).mappings()
        return [
            {
                "at": r["at"].isoformat() if hasattr(r["at"], "isoformat") else r["at"],
                "platform": r["action"].rsplit(".", 1)[-1],
                "outcome": r["outcome"],
                "sport": (r["detail"] or {}).get("sport"),
                "threshold_ids": (r["detail"] or {}).get("threshold_ids") or [],
                "error": (r["detail"] or {}).get("error"),
            }
            for r in rows
        ]


def history(ledger: list[dict[str, Any]], pushes: list[dict[str, Any]], sport: str, metric: str):
    """Every value this metric had, newest first, with the value it replaced."""
    rows = [r for r in ledger if r["sport"] == sport and r["metric"] == metric]
    by_successor = {r["superseded_by"]: r for r in rows if r.get("superseded_by")}
    out = []
    for row in sorted(rows, key=lambda r: (r.get("date_set") or "", r["id"]), reverse=True):
        previous = by_successor.get(row["id"])
        out.append(
            {
                **{k: row[k] for k in ("id", "value", "unit", "method", "date_set", "status")},
                "activity_id": row.get("activity_id"),
                "previous": previous["value"] if previous else None,
                "pushes": [
                    {k: p[k] for k in ("at", "platform", "outcome", "error")}
                    for p in pushes
                    if row["id"] in p["threshold_ids"]
                ],
            }
        )
    return out


def overview(
    engine: Engine,
    *,
    today: date,
    icu_settings: IcuSettings | None = None,
) -> dict[str, Any]:
    """Everything the Zones & thresholds page and the coach need, in one read."""
    ledger = _ledger(engine)
    current = current_values(engine)
    proposals = _proposals(engine, ledger)
    pushes = _pushes(engine)
    garmin = garmin_values(engine)
    icu: dict[str, dict[str, Any]] = {}
    # ``connected``: intervals.icu is set up at all. Never connected is not an error: the
    # page then says nothing; only a failed read of a connected account is a warning.
    icu_state: dict[str, Any] = {"ok": False, "error": None, "connected": True}
    fetch = icu_settings or icu_settings_live  # looked up now, so tests can replace it
    if icu_settings is None and not icu_connected():
        icu_state["connected"] = False
    else:
        try:
            icu = icu_values(fetch())
            icu_state["ok"] = True
        except Exception as exc:  # the page still works without intervals.icu
            log.info("intervals.icu sport settings not read: %s", exc)
            icu_state["error"] = "unavailable"
    sports = []
    for sport in SPORTS:
        plan = {m: row["value"] for m, row in (current.get(sport) or {}).items()}
        zones = derived_zones(sport, plan)
        platforms = {"garmin": garmin.get(sport) or {}, "intervals": icu.get(sport) or {}}
        metrics = []
        for metric, unit in SPORT_METRICS[sport]:
            row = (current.get(sport) or {}).get(metric)
            open_rows = [
                p for p in proposals.values() if p["sport"] == sport and p["metric"] == metric
            ]
            metrics.append(
                {
                    "metric": metric,
                    "unit": unit,
                    "plan": row,
                    "proposal": max(open_rows, key=lambda p: p["id"]) if open_rows else None,
                    "garmin": platforms["garmin"].get(metric),
                    "intervals": platforms["intervals"].get(metric),
                    "history": history(ledger, pushes, sport, metric),
                }
            )
        sports.append(
            {
                "sport": sport,
                "metrics": metrics,
                "zones": zones,
                "platforms": platforms,
                "mismatches": mismatches(sport, plan, zones, platforms),
                "highest_hr": _highest_hr(engine, sport, today),
                "vo2max": None,
            }
        )
    # Most-used sport first (training time in the last year); equal volumes keep SPORTS order.
    volume = _volume(engine, today)
    for item in sports:
        item["volume_s"] = volume.get(item["sport"], 0.0)
    sports.sort(key=lambda item: -item["volume_s"])
    refs = measured(engine, today)
    for item in sports:
        if item["sport"] == "run":
            item["vo2max"] = refs["vo2max_run"]
        elif item["sport"] == "bike":
            item["vo2max"] = refs["vo2max_bike"]
    general = []
    for metric, unit in GENERAL_METRICS:
        row = (current.get("general") or {}).get(metric)
        open_rows = [
            p for p in proposals.values() if p["sport"] == "general" and p["metric"] == metric
        ]
        general.append(
            {
                "metric": metric,
                "unit": unit,
                "plan": row,
                "proposal": max(open_rows, key=lambda p: p["id"]) if open_rows else None,
                "history": history(ledger, pushes, "general", metric),
            }
        )
    return {
        "sports": sports,
        "general": general,
        "measured": refs,
        "proposals": sorted(proposals.values(), key=lambda p: p["id"], reverse=True),
        "intervals": icu_state,
        "zwift": {"ftp": (current.get("bike") or {}).get("ftp", {}).get("value")},
    }


def for_coach(engine: Engine, *, today: date, icu_settings: IcuSettings | None = None):
    """Compact view for MCP: confirmed values, zones, open proposals and mismatches."""
    full = overview(engine, today=today, icu_settings=icu_settings)
    sports = {}
    for item in full["sports"]:
        sports[item["sport"]] = {
            "values": {
                m["metric"]: {
                    "value": m["plan"]["value"],
                    "unit": m["unit"],
                    "method": m["plan"]["method"],
                    "date_set": m["plan"]["date_set"],
                    "activity_id": m["plan"]["activity_id"],
                }
                for m in item["metrics"]
                if m["plan"]
            },
            "zones": item["zones"],
            "platforms": {
                name: {k: v for k, v in values.items() if k != "settings_id"}
                for name, values in item["platforms"].items()
            },
            "mismatches": item["mismatches"],
        }
    return {
        "sports": sports,
        "general": {g["metric"]: g["plan"] for g in full["general"] if g["plan"]},
        "measured": full["measured"],
        "proposals": [
            {
                k: p.get(k)
                for k in (
                    "id",
                    "sport",
                    "metric",
                    "value",
                    "unit",
                    "method",
                    "date_set",
                    "activity_id",
                    "summary",
                )
            }
            for p in full["proposals"]
        ],
        "intervals": full["intervals"],
    }


# Writes -----------------------------------------------------------------------------------


def _resolve_activity(engine: Engine, activity_id: str | None) -> str | None:
    if not activity_id:
        return None
    from vibesynergy.query import views

    text = str(activity_id).strip()
    if ":" not in text:
        return text  # already a group id
    return views.resolve_activity(engine, text)


def set_value(
    engine: Engine,
    *,
    sport: str,
    metric: str,
    value: float,
    method: str = "manual",
    date_set: date | None = None,
    activity_id: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Set a value directly (the athlete's own edit): stored and confirmed at once."""
    sport, metric = canonical(sport, metric)
    validate(sport, metric, value)
    unit = unit_for(sport, metric)
    current = (current_values(engine).get(sport) or {}).get(metric)
    when = date_set or today or utc_now().date()
    if (
        current
        and _same(metric, current["value"], value)
        and current.get("date_set") == when.isoformat()
    ):
        return current
    proposed = athlete_threshold.propose_threshold(
        engine,
        sport=sport,
        metric=metric,
        value=float(value),
        unit=unit,
        method=method,
        date_set=when,
        history=[
            {"event": "set", "by": "athlete", "activity_id": _resolve_activity(engine, activity_id)}
        ],
    )
    _supersede_aliases(engine, sport, metric)
    confirmed = athlete_threshold.confirm_threshold(engine, proposed["id"])
    return _row(confirmed_row(engine, confirmed["id"]))


def confirmed_row(engine: Engine, row_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(select(threshold_table).where(threshold_table.c.id == row_id))
            .mappings()
            .first()
        )
    if not row:
        raise LookupError("threshold row not found")
    return dict(row)


def _supersede_aliases(engine: Engine, sport: str, metric: str) -> None:
    """A current row under an old name (swim threshold_pace) gives way to the canonical one."""
    if (sport, metric) != ("swim", "css"):
        return
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            update(threshold_table)
            .where(
                and_(
                    threshold_table.c.sport == "swim",
                    threshold_table.c.metric == "threshold_pace",
                    threshold_table.c.status == "current",
                )
            )
            .values(status="superseded", updated_at=now)
        )


def _normalise_row(engine: Engine, row_id: int) -> None:
    """Store a proposal under its canonical metric and unit before it is confirmed."""
    row = confirmed_row(engine, row_id)
    sport, metric = canonical(row["sport"], row["metric"])
    values: dict[str, Any] = {}
    if (sport, metric) != (row["sport"], row["metric"]):
        values.update(sport=sport, metric=metric)
    if row["unit"] == "min/km":
        values.update(value=float(row["value"]) * 60, unit="s/km")
    if values:
        with engine.begin() as conn:
            conn.execute(
                update(threshold_table).where(threshold_table.c.id == row_id).values(**values)
            )
    _supersede_aliases(engine, sport, metric)


def _coach_entry_for(engine: Engine, row_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        for entry in conn.execute(
            select(coach_decision_log.c.id, coach_decision_log.c.meta).where(
                coach_decision_log.c.entry_type == "threshold_change"
            )
        ).mappings():
            if (entry["meta"] or {}).get("athlete_threshold_id") == row_id:
                return dict(entry)
    return None


def accept(engine: Engine, row_id: int) -> dict[str, Any]:
    """The athlete takes over a proposal: it becomes the plan's value."""
    from vibesynergy.query import coach_workouts

    row = confirmed_row(engine, row_id)
    if row["status"] == "current":
        return _row(row)
    if row["status"] != "proposed":
        raise ValueError("this proposal is no longer open")
    _normalise_row(engine, row_id)
    entry = _coach_entry_for(engine, row_id)
    if entry and (entry["meta"] or {}).get("status") == "proposed":
        coach_workouts.confirm_threshold_change(engine, entry["id"])
    else:
        athlete_threshold.confirm_threshold(engine, row_id)
    return _row(confirmed_row(engine, row_id))


def dismiss(engine: Engine, row_id: int) -> dict[str, Any]:
    """The athlete ignores a proposal; it stays in the history as dismissed."""
    row = confirmed_row(engine, row_id)
    if row["status"] == "dismissed":
        return _row(row)
    if row["status"] != "proposed":
        raise ValueError("this proposal is no longer open")
    now = utc_now()
    history_events = [*(row["history"] or []), {"at": now.isoformat(), "event": "dismissed"}]
    with engine.begin() as conn:
        conn.execute(
            update(threshold_table)
            .where(threshold_table.c.id == row_id)
            .values(status="dismissed", history=history_events, updated_at=now)
        )
    entry = _coach_entry_for(engine, row_id)
    if entry:
        meta = dict(entry["meta"] or {})
        meta.update(status="dismissed", dismissed_at=now.isoformat())
        with engine.begin() as conn:
            conn.execute(
                update(coach_decision_log)
                .where(coach_decision_log.c.id == entry["id"])
                .values(meta=meta)
            )
    return _row(confirmed_row(engine, row_id))


# Drift signals (task 11) ---------------------------------------------------------------

MAX_HR_MARGIN = 2  # bpm above the current max HR before a measured peak counts
RESTING_MARGIN = 3  # bpm between the 60-day resting HR and its baseline


def signals(engine: Engine, today: date) -> list[dict[str, Any]]:
    """Measured values that disagree with the current thresholds: a higher max HR in the last
    year, or a resting HR that moved from its baseline. Each says what and by how much."""
    current = current_values(engine)
    out: list[dict[str, Any]] = []
    for sport in ("run", "bike"):
        have = (current.get(sport) or {}).get("max_hr")
        peak = _highest_hr(engine, sport, today)
        if have and peak and peak["value"] > float(have["value"]) + MAX_HR_MARGIN:
            out.append(
                {
                    "sport": sport,
                    "metric": "max_hr",
                    "current": float(have["value"]),
                    "measured": float(peak["value"]),
                    "date": peak["date"],
                    "activity_id": peak["activity_id"],
                }
            )
    base = next(
        (v.get("resting_hr_baseline") for v in current.values() if v.get("resting_hr_baseline")),
        None,
    )
    resting = _mean_daily(engine, "hr.resting", today, 60)
    if base and resting and abs(resting["value"] - float(base["value"])) >= RESTING_MARGIN:
        out.append(
            {
                "sport": base["sport"],
                "metric": "resting_hr_baseline",
                "current": float(base["value"]),
                "measured": resting["value"],
                "days": resting["days"],
            }
        )
    return out
