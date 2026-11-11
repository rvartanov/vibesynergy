"""VibeSynergy's own training load per activity, validated against intervals.icu.

Formulas, fitted on the athlete's intervals.icu data (2025-06 to 2026-11):
- power: TSS = s · NP · IF / (FTP · 3600) · 100, IF = NP / FTP (bike; matches power_load);
- swim pace: s · IF³ / 3600 · 100, IF = speed / CSS speed (matches pace_load for Swim);
- run pace: s · IF² / 3600 · 100, IF = speed / threshold speed;
- heart rate: Banister TRIMP, normalised so one hour at LTHR is 100 (close to HRSS).
The choice per activity follows what intervals.icu picks in the athlete's data: power for rides
with power, else heart rate (also for swims and runs), else pace. ``compare`` reports the
difference with intervals.icu's ``icu_training_load`` per sport; nothing uses the own model
yet (intervals.icu leads).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any


def power_load(seconds: float, np_watts: float, ftp: float) -> float:
    intensity = np_watts / ftp
    return seconds * np_watts * intensity / (ftp * 3600) * 100


def pace_load(seconds: float, speed: float, threshold_speed: float, exponent: int) -> float:
    return seconds * (speed / threshold_speed) ** exponent / 3600 * 100


def _trimp(minutes: float, hr: float, rest: float, top: float) -> float:
    ratio = (hr - rest) / (top - rest)
    return minutes * ratio * 0.64 * math.exp(1.92 * ratio)


def hr_load(seconds: float, avg_hr: float, lthr: float, rest: float, top: float) -> float:
    if top <= rest or avg_hr <= rest:
        return 0.0
    return _trimp(seconds / 60, avg_hr, rest, top) / _trimp(60, lthr, rest, top) * 100


# A gap longer than this between two samples is a pause, not effort.
MAX_SAMPLE_GAP_S = 10.0


def hr_stream_load(
    times: list[float], hrs: list[float | None], lthr: float, rest: float, top: float
) -> float | None:
    """TRIMP integrated over the heart-rate stream, normalised so one hour at LTHR is 100.

    Interval sessions differ most from the average-based ``hr_load``: the exponential
    weighting makes a minute at 170 count far more than two minutes at 120.
    """
    if top <= rest or len(times) < 2:
        return None
    total = 0.0
    for i in range(1, len(times)):
        hr = hrs[i]
        dt = times[i] - times[i - 1]
        if hr is None or hr <= rest or dt <= 0:
            continue
        total += _trimp(min(dt, MAX_SAMPLE_GAP_S) / 60, hr, rest, top)
    return total / _trimp(60, lthr, rest, top) * 100


def from_icu(item: dict[str, Any]) -> tuple[str, float] | None:
    """Own load for one intervals.icu activity, from the inputs intervals.icu used."""
    seconds = item.get("moving_time")
    if not seconds:
        return None
    kind = str(item.get("type") or "")
    np_watts, ftp = item.get("icu_weighted_avg_watts"), item.get("icu_ftp")
    if "Ride" in kind and np_watts and ftp:
        return "power", power_load(seconds, np_watts, ftp)
    avg_hr, lthr = item.get("average_heartrate"), item.get("lthr")
    if avg_hr and lthr:
        rest = item.get("icu_resting_hr") or 45
        top = item.get("athlete_max_hr") or 180
        return "hr", hr_load(seconds, avg_hr, lthr, rest, top)
    speed, threshold = item.get("pace"), item.get("threshold_pace")
    if speed and threshold:
        return "pace", pace_load(seconds, speed, threshold, 3 if "Swim" in kind else 2)
    return None


def compare(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Own load against intervals.icu's per sport: count, median and P90 relative error."""
    errors: dict[str, list[float]] = defaultdict(list)
    for item in items:
        theirs = item.get("icu_training_load")
        mine = from_icu(item)
        if not theirs or mine is None:
            continue
        errors[str(item.get("type") or "?")].append(abs(mine[1] - theirs) / theirs)
    out = {}
    for sport, errs in sorted(errors.items()):
        errs.sort()
        out[sport] = {
            "n": len(errs),
            "median_pct": round(100 * statistics.median(errs), 1),
            "p90_pct": round(100 * errs[min(len(errs) - 1, int(len(errs) * 0.9))], 1),
        }
    return out


def compare_archive(engine: Any, raw: Any, window: Any) -> dict[str, Any]:
    """``compare`` over the intervals.icu activities stored in the archive for ``window``."""
    from vibesynergy.query import icu_fitness

    items: dict[Any, dict[str, Any]] = {}
    for payload in icu_fitness._payloads(engine, raw, "activities", window):  # pyright: ignore[reportPrivateUsage]
        for item in payload:
            items[item.get("id")] = item
    return compare(list(items.values()))


def _icu_start(item: dict[str, Any]) -> float | None:
    from datetime import UTC, datetime

    text = item.get("start_date")
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


def compare_streams(engine: Any, raw: Any, parquet: Any, window: Any) -> dict[str, Any]:
    """Heart-rate load from the stream against intervals.icu's, for activities where
    intervals.icu used heart rate: per sport the error of the average-based and the
    stream-based load."""
    from sqlalchemy import select

    from vibesynergy.query import icu_fitness
    from vibesynergy.store.schema import activity_unified as au

    items: dict[Any, dict[str, Any]] = {}
    for payload in icu_fitness._payloads(engine, raw, "activities", window):  # pyright: ignore[reportPrivateUsage]
        for item in payload:
            items[item.get("id")] = item
    with engine.connect() as conn:
        ours = conn.execute(
            select(au.c.start_utc, au.c.stream_key).where(
                au.c.local_date.between(window.start, window.end), au.c.stream_key.is_not(None)
            )
        ).all()
    starts = [(row.start_utc.timestamp(), row.stream_key) for row in ours if row.start_utc]
    errors: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"avg": [], "stream": []})
    for item in items.values():
        theirs = item.get("icu_training_load")
        mine = from_icu(item)
        start = _icu_start(item)
        if not theirs or mine is None or mine[0] != "hr" or start is None:
            continue
        key = next((k for t, k in starts if abs(t - start) <= 120), None)
        if key is None or not parquet.stream_path(key).exists():
            continue
        table = parquet.read_stream(key)
        if "hr" not in table.column_names:
            continue
        times = [v.timestamp() for v in table.column("t").to_pylist()]
        load = hr_stream_load(
            times,
            table.column("hr").to_pylist(),
            item["lthr"],
            item.get("icu_resting_hr") or 45,
            item.get("athlete_max_hr") or 180,
        )
        if load is None:
            continue
        sport = str(item.get("type") or "?")
        errors[sport]["avg"].append(abs(mine[1] - theirs) / theirs)
        errors[sport]["stream"].append(abs(load - theirs) / theirs)
    out = {}
    for sport, pair in sorted(errors.items()):
        row: dict[str, Any] = {"n": len(pair["avg"])}
        for name, errs in pair.items():
            errs.sort()
            row[f"{name}_median_pct"] = round(100 * statistics.median(errs), 1)
            row[f"{name}_p90_pct"] = round(100 * errs[min(len(errs) - 1, int(len(errs) * 0.9))], 1)
        out[sport] = row
    return out


def for_activity(engine: Any, raw: Any, parquet: Any, group_id: str) -> dict[str, Any] | None:
    """VibeSynergy's load for one activity next to intervals.icu's: power for rides with
    power, the heart-rate stream otherwise, the heart-rate average for pool swims (the
    stream is unreliable in the water). None when intervals.icu has no copy of it."""
    from datetime import timedelta

    from sqlalchemy import select

    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import icu_fitness
    from vibesynergy.store.schema import activity_unified as au

    with engine.connect() as conn:
        row = conn.execute(
            select(au.c.start_utc, au.c.local_date, au.c.stream_key, au.c.sport).where(
                au.c.group_id == group_id
            )
        ).first()
    if row is None or row.start_utc is None:
        return None
    start = row.start_utc.timestamp()
    window = Window(row.local_date - timedelta(days=1), row.local_date + timedelta(days=1))
    item = None
    for payload in icu_fitness._payloads(engine, raw, "activities", window):  # pyright: ignore[reportPrivateUsage]
        for candidate in payload:
            at = _icu_start(candidate)
            if at is not None and abs(at - start) <= 120:
                item = candidate
    if item is None or not item.get("icu_training_load"):
        return None
    mine = from_icu(item)
    if mine is None:
        return None
    method, value = mine
    kind = str(item.get("type") or "")
    if (
        method == "hr"
        and kind != "Swim"
        and row.stream_key
        and parquet.stream_path(row.stream_key).exists()
    ):
        table = parquet.read_stream(row.stream_key)
        if "hr" in table.column_names:
            stream = hr_stream_load(
                [v.timestamp() for v in table.column("t").to_pylist()],
                table.column("hr").to_pylist(),
                item["lthr"],
                item.get("icu_resting_hr") or 45,
                item.get("athlete_max_hr") or 180,
            )
            if stream is not None:
                method, value = "hr_stream", stream
    theirs = float(item["icu_training_load"])
    return {
        "intervals": round(theirs),
        "vibesynergy": round(value),
        "method": method,
        "difference_pct": round(100 * (value - theirs) / theirs),
    }
