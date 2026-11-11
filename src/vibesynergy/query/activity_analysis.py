"""Inside one activity and across similar ones, computed from the per-second streams.

``analyze``: a window of an activity (by time, distance or "the last N minutes") against the
rest of it: duration, distance, speed and pace, heart rate, power and normalized power,
cadence, climbing, efficiency factor, heart-rate drift, aerobic decoupling and time in
heart-rate zones.

``similar``: earlier sessions like this one, by ``profile`` (same kind of sport, duration and
intensity), ``route`` (the same course: start, finish and most of the track within 60 m) or
``workout`` (the same interval structure), each with a similarity score and the key
differences, so "am I getting faster on this loop" is one call.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime
from itertools import pairwise
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import plan as plan_q
from vibesynergy.query import views, workouts
from vibesynergy.query.units import presentation
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import activity_unified, zone_set

NP_SECONDS = 30  # normalized power: 4th-power mean of the 30-second rolling average
ROUTE_METRES = 60  # a track point counts as on the route within this distance
ROUTE_POINTS = 120  # points of the track compared
CANDIDATES = 400  # at most this many activities are looked at in full per search
ZONE_SPORT = {"run": "running", "ride": "cycling", "swim": "swimming"}


# Analyze -------------------------------------------------------------------------------------


def analyze(
    engine: Engine,
    parquet: ParquetStore,
    activity_id: str,
    *,
    start: str | float | None = None,
    end: str | float | None = None,
    last_minutes: float | None = None,
) -> dict[str, Any]:
    """The window [start, end) (elapsed time like 3600, '1:00:00' or '45min', or distance
    like '10km') of an activity, or its last N minutes; the whole activity when none."""
    detail = views.activity_detail(engine, parquet, activity_id)
    a = detail["activity"]
    cols = views.activity_streams(engine, parquet, activity_id, max_points=10**9)["columns"]
    elapsed = cols["elapsed_s"]
    if len(elapsed) < 2:
        raise ValueError(f"{activity_id} has too few samples to analyse")
    total = elapsed[-1]
    if last_minutes is not None:
        lo, hi = max(0.0, total - float(last_minutes) * 60), total + 1
    else:
        lo = _position(start, cols) if start is not None else 0.0
        hi = _position(end, cols) if end is not None else total + 1
    if hi <= lo:
        raise ValueError("the window ends before it starts")
    inside = [i for i, t in enumerate(elapsed) if lo <= t < hi]
    if len(inside) < 2:
        raise ValueError("no samples in that window")
    outside = [i for i, t in enumerate(elapsed) if not lo <= t < hi]
    zones = _hr_zones(engine, a["sport"])
    window = _stats(cols, inside, zones)
    rest = _stats(cols, outside, zones) if len(outside) >= 2 else None
    for stats in (window, rest):
        if stats is not None:
            stats["sport_units"] = presentation(a["sport"], stats)
    compare = None
    if rest:
        compare = {
            k: round(window[k] - rest[k], 3)
            for k in ("avg_hr", "avg_power", "norm_power", "avg_speed", "avg_cadence", "ef")
            if isinstance(window.get(k), int | float) and isinstance(rest.get(k), int | float)
        }
    return {
        "activity": a["group_id"],
        "sport": a["sport"],
        "name": a.get("name"),
        "date": str(a["local_date"]),
        "window": {"from_s": round(lo, 1), "to_s": round(min(hi, total), 1), **window},
        "rest_of_activity": rest,
        "window_minus_rest": compare,
        "zones": zones,
        "notes": [
            "ef: efficiency factor, normalized power (or speed in m/s times 100 without power) per "
            "heartbeat per minute.",
            "hr_drift: average heart rate of the second half of the window minus the first.",
            "decoupling_pct: how much efficiency dropped from the first half to the second; "
            "under about 5% counts as aerobically steady.",
            "Grade-adjusted pace is not computed.",
        ],
    }


def _position(value: str | float, cols: dict[str, list]) -> float:
    """Elapsed seconds for 3600, '1:00:00', '45:00', '45min', '1.5h' or a distance '10km' /
    '800m' (the first moment that distance was reached)."""
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().lower().replace(" ", "")
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)(km|m)", text):
        metres = float(m.group(1)) * (1000 if m.group(2) == "km" else 1)
        dist = cols.get("dist")
        if not dist:
            raise ValueError("this activity has no distance data; give a time instead")
        for t, d in zip(cols["elapsed_s"], dist, strict=True):
            if d is not None and d >= metres:
                return float(t)
        raise ValueError(f"the activity is shorter than {value}")
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)(min|h|s)", text):
        return float(m.group(1)) * {"min": 60, "h": 3600, "s": 1}[m.group(2)]
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + int(part)
        return seconds
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    raise ValueError(f"can't read {value!r}: use seconds, h:mm:ss, 45min or 10km")


def _values(cols: dict[str, list], name: str, idx: list[int]) -> list[float]:
    col = cols.get(name) or []
    return [col[i] for i in idx if i < len(col) and col[i] is not None]


def _stats(cols: dict[str, list], idx: list[int], zones: list[dict] | None) -> dict[str, Any]:
    elapsed = cols["elapsed_s"]
    seconds = elapsed[idx[-1]] - elapsed[idx[0]]
    dt = statistics.median(b - a for a, b in pairwise(elapsed)) or 1
    out: dict[str, Any] = {"seconds": round(seconds, 1)}
    dist = _values(cols, "dist", [idx[0], idx[-1]])
    distance = dist[-1] - dist[0] if len(dist) == 2 else None
    if distance:
        out["distance_m"] = round(distance, 1)
        speed = distance / seconds if seconds else None
        out["avg_speed"] = round(speed, 3) if speed else None
    hr = _values(cols, "hr", idx)
    if hr:
        out["avg_hr"] = round(statistics.fmean(hr), 1)
        out["max_hr"] = max(hr)
    power = _values(cols, "power", idx)
    if power and any(power):
        out["avg_power"] = round(statistics.fmean(power), 1)
        out["max_power"] = max(power)
        out["norm_power"] = _normalized_power(power, dt)
    cadence = [c for c in _values(cols, "cadence", idx) if c > 0]
    if cadence:
        out["avg_cadence"] = round(statistics.fmean(cadence), 1)
    alt = _values(cols, "alt", idx)
    if len(alt) > 1:
        out["ascent_m"] = round(_ascent(alt), 1)
    # Efficiency, drift and decoupling need heart rate and an output (power or speed).
    half = len(idx) // 2
    first, second = idx[:half], idx[half:]

    def ef(part: list[int]) -> float | None:
        beats = _values(cols, "hr", part)
        if not beats:
            return None
        watts = _values(cols, "power", part)
        if watts and any(watts):
            output = _normalized_power(watts, dt)
        else:
            speed = _values(cols, "speed", part)
            output = statistics.fmean(speed) * 100 if speed else None
        return output / statistics.fmean(beats) if output else None

    if hr:
        out["ef"] = _round(ef(idx), 3)
        h1, h2 = _values(cols, "hr", first), _values(cols, "hr", second)
        if h1 and h2:
            out["hr_drift_bpm"] = round(statistics.fmean(h2) - statistics.fmean(h1), 1)
        e1, e2 = ef(first), ef(second)
        if e1 and e2:
            out["decoupling_pct"] = round(100 * (e1 - e2) / e1, 1)
    if zones and hr:
        out["time_in_hr_zones_s"] = _zone_time(hr, dt, zones)
    return out


def _normalized_power(power: list[float], dt: float) -> float:
    span = max(1, round(NP_SECONDS / dt))
    if len(power) < span:
        return round(statistics.fmean(power), 1)
    rolling, total = [], sum(power[:span])
    rolling.append(total / span)
    for i in range(span, len(power)):
        total += power[i] - power[i - span]
        rolling.append(total / span)
    return round(statistics.fmean(p**4 for p in rolling) ** 0.25, 1)


def _ascent(alt: list[float], hysteresis: float = 1.0) -> float:
    """Metres climbed, ignoring wobbles smaller than ``hysteresis`` (barometer noise)."""
    gain, ref = 0.0, alt[0]
    for v in alt[1:]:
        if v > ref + hysteresis:
            gain += v - ref
            ref = v
        elif v < ref - hysteresis:
            ref = v
    return gain


def _hr_zones(engine: Engine, sport: str | None) -> list[dict] | None:
    """The current heart-rate zones for the sport (else the default set) as [low, high)."""
    want = ZONE_SPORT.get(plan_q.family(sport) or "", "default")
    with engine.connect() as conn:
        rows = conn.execute(
            select(zone_set.c.sport, zone_set.c.boundaries)
            .where(zone_set.c.kind == "hr")
            .order_by(zone_set.c.valid_from.desc())
        ).all()
    bounds = next((b for s, b in rows if s == want), None) or next(
        (b for s, b in rows if s == "default"), None
    )
    if not bounds or len(bounds) < 2:
        return None
    return [
        {"zone": i + 1, "low": bounds[i], "high": bounds[i + 1]} for i in range(len(bounds) - 1)
    ]


def _zone_time(hr: list[float], dt: float, zones: list[dict]) -> dict[str, float]:
    out = {"below": 0.0, **{str(z["zone"]): 0.0 for z in zones}}
    for v in hr:
        zone = next((z for z in reversed(zones) if v >= z["low"]), None)
        out[str(zone["zone"]) if zone else "below"] += dt
    return {k: round(v) for k, v in out.items()}


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


# Similar -------------------------------------------------------------------------------------

KEY_FIELDS = ("moving_s", "distance_m", "avg_speed", "avg_hr", "avg_power", "ascent_m")


def _candidate_day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def similar(
    engine: Engine,
    parquet: ParquetStore,
    activity_id: str,
    *,
    by: str = "profile",
    window: Window | None = None,
    limit: int = 10,
    candidates: list[dict] | None = None,
    group: str | None = None,
    target: dict | None = None,
) -> dict[str, Any]:
    if by not in ("profile", "route", "workout"):
        raise ValueError("by: profile, route or workout")
    if group is None:
        if target is not None:
            group = target["group_id"]
        else:
            group = views.resolve_activity(engine, activity_id)
    if target is None:
        if candidates is not None:
            target = next((row for row in candidates if row["group_id"] == group), None)
        if target is None:
            target = _activity_row(engine, group)
    if candidates is None:
        query = select(activity_unified).where(activity_unified.c.group_id != group)
        if window:
            query = query.where(activity_unified.c.local_date.between(window.start, window.end))
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(query)]
    else:
        # Callers pass local_date index order, already limited to the window when they can.
        rows = []
        for row in candidates:
            if row["group_id"] == group:
                continue
            if window is not None:
                day = row["local_date"]
                if not isinstance(day, date):
                    day = _candidate_day(day)
                if not window.start <= day <= window.end:
                    continue
            rows.append(row)
    fam = plan_q.family(target["sport"])
    pool = [r for r in rows if plan_q.family(r["sport"]) == fam and r["sport"]]
    if fam is None:
        pool = [r for r in rows if r["sport"] == target["sport"]]
    if by == "profile":
        scored = [(s, r) for r in pool if (s := _profile_score(target, r)) is not None]
    elif by == "route":
        scored = _route_matches(parquet, target, pool)
    else:
        scored = _workout_matches(engine, parquet, target, pool)
    scored.sort(key=lambda x: (-x[0], str(x[1]["local_date"])))
    matches = [
        {
            "id": (r["sources"] or [r["group_id"]])[0],
            "date": str(r["local_date"]),
            "name": r["name"],
            "sport": r["sport"],
            "score": round(score, 3),
            **{k: r.get(k) for k in KEY_FIELDS},
            "vs_this": _deltas(r, target),
        }
        for score, r in scored[: max(1, min(limit, 50))]
    ]
    return {
        "activity": group,
        "by": by,
        "this": {"date": str(target["local_date"]), **{k: target.get(k) for k in KEY_FIELDS}},
        "matches": matches,
        "method": {
            "profile": "same kind of sport; score from the differences in moving time, "
            "distance and average heart rate (1 = identical)",
            "route": f"start and finish within {ROUTE_METRES * 3} m and the share of "
            f"{ROUTE_POINTS} points of this track within {ROUTE_METRES} m of the other "
            "(both ways)",
            "workout": "same number of work intervals (laps split by power or speed); score "
            "from the differences in interval durations and moving time",
        }[by],
        "note": "vs_this: the match minus this activity (positive avg_speed: faster there).",
    }


def _activity_row(engine: Engine, group: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            select(activity_unified).where(activity_unified.c.group_id == group)
        ).first()
    if not row:
        raise ValueError(f"no activity {group}")
    return dict(row._mapping)


def _deltas(row: dict, target: dict) -> dict[str, float]:
    return {
        k: round(row[k] - target[k], 3)
        for k in KEY_FIELDS
        if isinstance(row.get(k), int | float) and isinstance(target.get(k), int | float)
    }


def _closeness(a: float | None, b: float | None, scale: float) -> float | None:
    """1 when equal, 0 at a relative difference of ``scale`` or more."""
    if not a or not b:
        return None
    return max(0.0, 1 - abs(a - b) / max(abs(a), abs(b)) / scale)


def _profile_score(target: dict, row: dict) -> float | None:
    parts = [
        (_closeness(target["moving_s"], row["moving_s"], 0.3), 2),
        (_closeness(target["distance_m"], row["distance_m"], 0.3), 2),
        (_closeness(target["avg_hr"], row["avg_hr"], 0.15), 1),
    ]
    used = [(v, w) for v, w in parts if v is not None]
    if not used or parts[0][0] is None or parts[0][0] == 0:
        return None
    score = sum(v * w for v, w in used) / sum(w for _, w in used)
    return score if score > 0.3 else None


def _track(parquet: ParquetStore, key: str | None) -> list[tuple[float, float]]:
    if not key or not parquet.stream_path(key).exists():
        return []
    table = parquet.read_stream(key)
    if "lat" not in table.column_names or "lon" not in table.column_names:
        return []
    points = [
        (la, lo)
        for la, lo in zip(
            table.column("lat").to_pylist(), table.column("lon").to_pylist(), strict=True
        )
        if la is not None and lo is not None and (la, lo) != (0, 0)
    ]
    if len(points) <= ROUTE_POINTS:
        return points
    step = len(points) / ROUTE_POINTS
    return [points[int(i * step)] for i in range(ROUTE_POINTS)] + [points[-1]]


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return 6_371_000 * math.hypot(dx, dy)


def _overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Share of the points of ``a`` within ROUTE_METRES of some point of ``b`` (grid)."""
    cell = ROUTE_METRES / 111_000  # degrees of latitude; longitude cells are only wider
    grid: dict[tuple[int, int], list] = {}
    for p in b:
        grid.setdefault((int(p[0] / cell), int(p[1] / cell)), []).append(p)
    hits = 0
    for p in a:
        ci, cj = int(p[0] / cell), int(p[1] / cell)
        near = (
            q
            for di in (-1, 0, 1)
            for dj in (-2, -1, 0, 1, 2)
            for q in grid.get((ci + di, cj + dj), ())
        )
        if any(_metres(p, q) <= ROUTE_METRES for q in near):
            hits += 1
    return hits / len(a) if a else 0.0


def _route_matches(parquet: ParquetStore, target: dict, pool: list[dict]) -> list:
    mine = _track(parquet, target["stream_key"])
    if len(mine) < 10:
        raise ValueError("this activity has no GPS track to match")
    dist = target["distance_m"] or 0
    close = [
        r for r in pool if r["stream_key"] and (not dist or _closeness(dist, r["distance_m"], 1))
    ]
    close.sort(key=lambda r: abs((r["distance_m"] or 0) - dist))
    out = []
    for r in close[:CANDIDATES]:
        if dist and r["distance_m"] and abs(r["distance_m"] - dist) / dist > 0.25:
            continue
        other = _track(parquet, r["stream_key"])
        if len(other) < 10:
            continue
        if _metres(mine[0], other[0]) > ROUTE_METRES * 3:
            continue
        if _metres(mine[-1], other[-1]) > ROUTE_METRES * 3:
            continue
        score = min(_overlap(mine, other), _overlap(other, mine))
        if score >= 0.7:
            out.append((score, r))
    return out


def _work_durations(engine: Engine, parquet: ParquetStore, group: str) -> list[float] | None:
    try:
        result = workouts.intervals(engine, parquet, group)
    except ValueError:
        return None
    work = [lap["seconds"] for lap in result.get("laps", []) if lap.get("kind") == "work"]
    return work or None


def _workout_matches(engine: Engine, parquet: ParquetStore, target: dict, pool: list) -> list:
    mine = _work_durations(engine, parquet, target["group_id"])
    if not mine:
        raise ValueError("this activity has no interval structure (its laps are steady)")
    moving = target["moving_s"] or 0
    close = [
        r for r in pool if not moving or not r["moving_s"] or abs(r["moving_s"] - moving) < moving
    ]
    close.sort(key=lambda r: abs((r["moving_s"] or 0) - moving))
    out = []
    for r in close[:CANDIDATES]:
        other = _work_durations(engine, parquet, r["group_id"])
        if not other or len(other) != len(mine):
            continue
        parts = [_closeness(a, b, 0.5) or 0 for a, b in zip(mine, other, strict=True)]
        score = 0.8 * statistics.fmean(parts) + 0.2 * (_closeness(moving, r["moving_s"], 1) or 0)
        if score >= 0.5:
            out.append((score, r))
    return out
