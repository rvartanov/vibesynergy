"""Race predictions for 5 km, 10 km, 10 miles, half marathon, marathon and 50 km (#9).

Two kinds of evidence, each a time over a distance:

- **Threshold pace** (Zones & thresholds): by definition the pace you can hold for about an
  hour, so one hour at that pace is an effort to predict from. It comes from a test or a race
  and is kept current, so it reflects fitness also in weeks of only easy running.
- **Recent efforts**: your fastest time over 1 mile to the marathon in the run streams of the
  last 12 weeks (``training.pace_curve``). Mostly training, so usually slower than racing; a
  race or a test shows up here and then counts.

Each distance is predicted from every effort with Riegel's formula T2 = T1 · (D2 / D1)^k and
the fastest prediction counts (a slower one is an easier run, not less fitness), using only
efforts within 4.5x of the distance when there are any. k is Riegel's 1.06 up to the half
marathon and 1.08 beyond it: over the marathon 1.06 is too optimistic for recreational
runners (Vickers & Vertosick, BMC Sports Sci Med Rehabil 2016). Garmin's own prediction is
shown next to it where Garmin has one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.store.schema import daily_unified

# (key, distance in metres, Garmin's metric for it)
TARGETS = (
    ("5k", 5000.0, "fitness.race_pred.5k"),
    ("10k", 10000.0, "fitness.race_pred.10k"),
    ("10mi", 16093.44, None),
    ("half", 21097.5, "fitness.race_pred.half"),
    ("marathon", 42195.0, "fitness.race_pred.marathon"),
    ("50k", 50000.0, None),
)
# The efforts looked for in the run streams (shorter ones say little about endurance).
EFFORTS = (1609.34, 3000.0, 5000.0, 10000.0, 16093.44, 21097.5, 30000.0, 42195.0)
WINDOW_DAYS = 84  # 12 weeks of efforts
TREND_DAYS = 42  # the change against six weeks ago
RIEGEL = 1.06
LONG = 1.08  # beyond the half marathon
HALF = 21097.5
MAX_STRETCH = 4.5


def riegel_split(seconds: float, from_m: float, to_m: float) -> float:
    """Riegel with 1.06 up to the half marathon and 1.08 beyond it (either direction)."""
    lo, hi = sorted((from_m, to_m))
    factor = 1.0
    if lo < HALF:
        factor *= (min(hi, HALF) / lo) ** RIEGEL
    if hi > HALF:
        factor *= (hi / max(lo, HALF)) ** LONG
    return seconds * factor if to_m >= from_m else seconds / factor


def certainty(ratio: float, age_days: int) -> str:
    """high: within twice the effort's distance and recent; low: far beyond it or old."""
    stretch = max(ratio, 1 / ratio)
    if stretch <= 2.0 and age_days <= 42:
        return "high"
    if stretch <= 4.5 and age_days <= 84:
        return "medium"
    return "low"


def predict(efforts: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Predictions from efforts ``{distance_m, seconds, date, kind, activity?}``."""
    efforts = [e for e in efforts if e.get("seconds") and e.get("distance_m")]
    rows = []
    for key, metres, _garmin in TARGETS:
        near = [
            e
            for e in efforts
            if max(metres, e["distance_m"]) / min(metres, e["distance_m"]) <= MAX_STRETCH
        ]
        pool = near or efforts
        if not pool:
            rows.append({"key": key, "distance_m": metres, "seconds": None})
            continue
        options = [(riegel_split(e["seconds"], e["distance_m"], metres), e) for e in pool]
        seconds, source = min(options, key=lambda o: o[0])
        age = (today - date.fromisoformat(str(source["date"])[:10])).days
        rows.append(
            {
                "key": key,
                "distance_m": metres,
                "seconds": round(seconds),
                "pace_s_per_km": round(seconds / metres * 1000, 1),
                "certainty": certainty(metres / source["distance_m"], age),
                "from": {
                    "kind": source.get("kind", "effort"),
                    "distance_m": round(source["distance_m"], 1),
                    "seconds": source["seconds"],
                    "date": str(source["date"])[:10],
                    "activity": source.get("activity"),
                },
            }
        )
    return {"predictions": rows}


def _threshold(engine: Engine, on: date) -> dict[str, Any] | None:
    """One hour at the run threshold pace valid on ``on`` (s/km), as an effort."""
    from vibesynergy.query import training

    set_on = _threshold_date(engine, on)
    if set_on is None:
        return None  # not known yet on that day (never projected backwards)
    pace = training.ThresholdTimeline(engine).value("run", "threshold_pace", on)
    if not pace:
        return None
    return {
        "kind": "threshold",
        "distance_m": 3600 / float(pace) * 1000,
        "seconds": 3600.0,
        "date": str(set_on or on),
        "pace_s_per_km": float(pace),
    }


def _threshold_date(engine: Engine, on: date) -> date | None:
    from vibesynergy.query import thresholds

    rows = [
        r
        for r in thresholds._ledger(engine)  # pyright: ignore[reportPrivateUsage]
        if r["sport"] == "run" and r["metric"] == "threshold_pace" and r["date_set"]
    ]
    dates = sorted(date.fromisoformat(str(r["date_set"])) for r in rows)
    past = [d for d in dates if d <= on]
    return past[-1] if past else None


def _garmin(engine: Engine, today: date) -> dict[str, dict[str, Any]]:
    """Garmin's latest prediction per distance, from the last 14 days."""
    out = {}
    with engine.connect() as conn:
        for key, _metres, metric in TARGETS:
            if metric is None:
                continue
            row = conn.execute(
                select(daily_unified.c.date, daily_unified.c.value)
                .where(
                    daily_unified.c.metric == metric,
                    daily_unified.c.date.between(today - timedelta(days=13), today),
                )
                .order_by(daily_unified.c.date.desc())
                .limit(1)
            ).first()
            if row is not None:
                out[key] = {"seconds": round(row.value), "date": str(row.date)}
    return out


def _efforts(engine: Engine, parquet, window: Window) -> list[dict[str, Any]]:
    from vibesynergy.query import training

    curve = training.pace_curve(engine, parquet, window, sport="run", distances=EFFORTS)
    return [
        {
            "kind": "effort",
            "distance_m": e["distance_m"],
            "seconds": e["seconds"],
            "date": e["date"],
            "activity": e["activity"],
        }
        for e in curve["curve"]
    ]


def evidence(engine: Engine, parquet, on: date) -> list[dict[str, Any]]:
    window = Window(on - timedelta(days=WINDOW_DAYS - 1), on)
    found = _efforts(engine, parquet, window)
    if (threshold := _threshold(engine, on)) is not None:
        found.append(threshold)
    return found


def race_predictions(engine: Engine, parquet, today: date) -> dict[str, Any]:
    """Predictions now, the change against six weeks ago, and Garmin's next to them."""
    now = predict(evidence(engine, parquet, today), today)
    then_day = today - timedelta(days=TREND_DAYS)
    then = predict(evidence(engine, parquet, then_day), then_day)
    # A change only between predictions from the same kind of evidence (threshold or effort).
    earlier = {
        r["key"]: {"seconds": r["seconds"], "kind": r["from"]["kind"]}
        for r in then["predictions"]
        if r["seconds"]
    }
    garmin = _garmin(engine, today)
    for row in now["predictions"]:
        before = earlier.get(row["key"])
        same = before is not None and before["kind"] == (row.get("from") or {}).get("kind")
        row["change_s"] = (
            row["seconds"] - before["seconds"]
            if before is not None and row["seconds"] and same
            else None
        )
        row["garmin"] = garmin.get(row["key"])
    return {
        **now,
        "window_days": WINDOW_DAYS,
        "trend_days": TREND_DAYS,
        "method": "riegel 1.06, 1.08 beyond the half marathon",
    }
