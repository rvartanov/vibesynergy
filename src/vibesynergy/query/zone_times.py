"""Time in zones per activity, counted by VibeSynergy from the stream (12-11-2026).

- Heart rate: the five zones the watch had for that sport on the activity's date (Garmin's
  zone history; for running, cycling and the rest VibeSynergy keeps Friel's zones from LTHR
  there, 5a-5c as zone 5; swimming has Garmin's own). Without that history, Friel's zones from
  the LTHR of that date. Time below zone 1 counts in no zone, as on the watch.
- Power (rides): Coggan's seven zones from the FTP of that date.
- Pace (runs): Friel's seven run-pace zones from the threshold pace of that date. Pool swims
  have no speed stream, so they have no pace zones.

Each sample counts for the time to the next one, at most ``MAX_GAP_S`` (a pause is not time in
a zone). Results are kept in ``activity_zone_own`` and counted again only when the stream or the
thresholds change.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.query import thresholds
from vibesynergy.store.schema import activity_unified, activity_zone_own

KINDS = ("hr", "power", "pace")
MAX_GAP_S = 10.0
VERSION = "v2"


def seconds_by_zone(
    times: list[float], values: list[float | None], floors: list[float]
) -> dict[int, float]:
    """Seconds per zone (1-based). ``floors`` are where each zone starts, low to high; a value
    below the first is in no zone."""
    out: dict[int, float] = {}
    for i in range(len(times) - 1):
        value = values[i]
        dt = times[i + 1] - times[i]
        if value is None or value <= 0 or dt <= 0:
            continue
        zone = bisect_right(floors, value)
        if zone:
            out[zone] = out.get(zone, 0.0) + min(dt, MAX_GAP_S)
    return out


def _from_uppers(uppers: list[float]) -> list[float]:
    """Floors for zones given by their upper bounds (an upper bound is still in its zone)."""
    return [0.0, *(u + 1e-6 for u in uppers)] if uppers else []


def _hr_uppers(fam: str, lthr: float | None, max_hr: float | None) -> list[float]:
    zones = thresholds.hr_zones("bike" if fam == "ride" else "run", lthr, max_hr)
    highs = [z["high"] for z in zones[:4] if z["high"] is not None]  # Z1-Z4; above is Z5
    return [float(h) for h in highs] if len(highs) == 4 else []


def _power_uppers(ftp: float | None) -> list[float]:
    return [float(z["high"]) for z in thresholds.power_zones(ftp) if z["high"] is not None]


def _pace_uppers(threshold_pace: float | None) -> list[float]:
    """Run pace zones as upper bounds on speed (m/s): zone 1 is the slowest."""
    edges = [z["fast"] for z in thresholds.pace_zones("run", threshold_pace) if z["fast"]]
    return [1000 / float(edge) for edge in edges]  # s/km edge -> the speed it is reached at


_GARMIN_HR_SPORT = {"ride": "cycling", "run": "running", "swim": "swimming"}


def garmin_hr_history(engine: Engine) -> dict[str, list[tuple[Any, list[float]]]]:
    """Garmin's heart-rate zone floors per sport over time: [(valid_from, [Z1..Z5 floors])]."""
    from vibesynergy.store.schema import zone_set

    out: dict[str, list[tuple[Any, list[float]]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(zone_set.c.sport, zone_set.c.valid_from, zone_set.c.boundaries)
            .where(zone_set.c.source == "garmin", zone_set.c.kind == "hr")
            .order_by(zone_set.c.valid_from)
        ).all()
    for row in rows:
        floors = [float(b) for b in (row.boundaries or [])[:5]]
        if len(floors) == 5 and floors == sorted(floors):
            out.setdefault(row.sport, []).append((row.valid_from, floors))
    return out


def _garmin_floors(history, fam: str, day) -> list[float]:
    points = history.get(_GARMIN_HR_SPORT.get(fam, "default")) or []
    found = None
    for when, floors in points:
        if when > day:
            break
        found = floors
    return found or (points[0][1] if points else [])


def bounds(kind: str, fam: str, inputs: Any, *, history=None, day=None) -> list[float]:
    """Where each zone starts (floors), for one activity of family ``fam``."""
    if kind == "hr":
        if history and day is not None and (floors := _garmin_floors(history, fam, day)):
            return floors
        lthr = inputs.lthr.get(fam) or inputs.lthr.get("run")
        return _from_uppers(_hr_uppers(fam, lthr, inputs.top(fam) or None))
    if kind == "power":
        return _from_uppers(_power_uppers(inputs.ftp)) if fam == "ride" else []
    if kind == "pace":
        return _from_uppers(_pace_uppers(inputs.threshold_pace)) if fam == "run" else []
    raise ValueError("kind must be hr, power or pace")


_COLUMN = {"hr": "hr", "power": "power", "pace": "speed"}


def for_window(
    engine: Engine,
    parquet,
    window: Window,
    kind: str,
    athlete: dict[str, Any] | None = None,
) -> dict[str, dict[int, float]]:
    """{group id: {zone: seconds}} for the activities in ``window`` that have a stream with
    ``kind`` and thresholds for it; kept and reused until the stream or thresholds change."""
    from vibesynergy.query import training
    from vibesynergy.query.plan import family

    if kind not in KINDS:
        raise ValueError("kind must be hr, power or pace")
    timeline = training.ThresholdTimeline(engine, athlete)
    history = garmin_hr_history(engine) if kind == "hr" else None
    t = activity_unified
    with engine.connect() as conn:
        rows = conn.execute(
            select(t.c.group_id, t.c.local_date, t.c.sport, t.c.stream_key).where(
                t.c.local_date.between(window.start, window.end), t.c.stream_key.is_not(None)
            )
        ).all()
        stored = {
            r.group_id: r
            for r in conn.execute(
                select(activity_zone_own).where(
                    activity_zone_own.c.kind == kind,
                    activity_zone_own.c.group_id.in_([r.group_id for r in rows]),
                )
            )
        }
    out: dict[str, dict[int, float]] = {}
    fresh = []
    for row in rows:
        fam = family(row.sport) or "other"
        floors = bounds(kind, fam, timeline.on(row.local_date), history=history, day=row.local_date)
        if not floors:
            continue
        key = hashlib.sha256(
            f"{row.stream_key}|{kind}|{fam}|{floors}|{VERSION}".encode()
        ).hexdigest()[:24]
        hit = stored.get(row.group_id)
        if hit is not None and hit.inputs == key:
            zones = {int(z): float(s) for z, s in (hit.zones or {}).items()}
        else:
            zones = {}
            if parquet is not None and parquet.stream_path(row.stream_key).exists():
                table = parquet.read_stream(row.stream_key)
                column = _COLUMN[kind]
                if column in table.column_names:
                    times = [v.timestamp() for v in table.column("t").to_pylist()]
                    zones = seconds_by_zone(times, table.column(column).to_pylist(), floors)
            fresh.append(
                {"group_id": row.group_id, "kind": kind, "inputs": key,
                 "zones": {str(z): round(s, 1) for z, s in zones.items()},
                 "computed_at": utc_now()}
            )  # fmt: skip
        if zones:
            out[row.group_id] = zones
    if fresh and parquet is not None:
        with engine.begin() as conn:
            for start in range(0, len(fresh), 500):
                statement = insert(activity_zone_own).values(fresh[start : start + 500])
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=["group_id", "kind"],
                        set_={k: statement.excluded[k] for k in ("inputs", "zones", "computed_at")},
                    )
                )
    return out
