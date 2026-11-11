"""Best efforts for every sport: per sport and all sports together, over a window.

Run: fastest time per distance (the pace curve) and the longest run. Bike: best power per
duration (the power curve) and the longest ride. Swim: the longest swim and the fastest
session pace over at least 1000 m (session averages, not single laps: SWIMDQ-1). Other
sports: the longest session. All together: the longest session, the most hours in one
week and the biggest training load in one day. Coaching sessions you give are left out.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import plan as plan_q
from vibesynergy.query import training
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import activity_unified

SPORTS = ("all", "run", "bike", "swim", "other")
_FAMILY = {"run": "run", "ride": "bike", "swim": "swim"}


def _ref(row) -> dict[str, Any]:
    return {
        "activity": row.group_id,
        "date": row.local_date.isoformat(),
        "name": row.name,
        "sport": row.sport,
    }


def _rows(engine: Engine, window: Window) -> list:
    t = activity_unified
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    t.c.group_id,
                    t.c.local_date,
                    t.c.name,
                    t.c.sport,
                    t.c.moving_s,
                    t.c.elapsed_s,
                    t.c.distance_m,
                    t.c.training_load,
                ).where(
                    t.c.local_date.between(window.start, window.end),
                    t.c.kind.is_distinct_from("coaching"),
                )
            ).all()
        )


def _group(row) -> str:
    return _FAMILY.get(plan_q.family(row.sport) or "", "other")


def _longest(rows: list) -> dict[str, Any] | None:
    by_time = max(rows, key=lambda r: r.moving_s or r.elapsed_s or 0, default=None)
    by_distance = max(rows, key=lambda r: r.distance_m or 0, default=None)
    if by_time is None:
        return None
    out: dict[str, Any] = {
        "longest_time": {**_ref(by_time), "seconds": by_time.moving_s or by_time.elapsed_s}
    }
    if by_distance is not None and (by_distance.distance_m or 0) > 0:
        out["longest_distance"] = {**_ref(by_distance), "metres": by_distance.distance_m}
    return out


def overview(
    engine: Engine, parquet: ParquetStore, window: Window, sport: str = "all"
) -> dict[str, Any]:
    if sport not in SPORTS:
        raise ValueError(f"sport: one of {', '.join(SPORTS)}")
    rows = _rows(engine, window)
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[_group(row)].append(row)
    out: dict[str, Any] = {"sport": sport, "start": str(window.start), "end": str(window.end)}
    if sport in ("all", "run"):
        run = {**(_longest(groups["run"]) or {})}
        run["fastest"] = training.pace_curve(engine, parquet, window).get("curve", [])
        out["run"] = run
    if sport in ("all", "bike"):
        bike = {**(_longest(groups["bike"]) or {})}
        bike["power"] = training.power_curve(engine, parquet, window, sport="ride").get("curve", [])
        out["bike"] = bike
    if sport in ("all", "swim"):
        swim = {**(_longest(groups["swim"]) or {})}
        # At least 1000 m: short sessions carry lap-detection artefacts (SWIMDQ-1).
        paced = [r for r in groups["swim"] if (r.distance_m or 0) >= 1000 and r.moving_s]
        fastest = min(paced, key=lambda r: r.moving_s / r.distance_m, default=None)
        if fastest is not None:
            swim["fastest_session"] = {
                **_ref(fastest),
                "s_per_100m": round(fastest.moving_s / fastest.distance_m * 100, 1),
                "metres": fastest.distance_m,
                "basis": "session_average",
            }
        out["swim"] = swim
    if sport in ("all", "other"):
        out["other"] = _longest(groups["other"]) or {}
    if sport == "all":
        out["together"] = _together(rows)
    return out


def _together(rows: list) -> dict[str, Any]:
    if not rows:
        return {}
    week: dict[Any, float] = defaultdict(float)
    day_load: dict[Any, float] = defaultdict(float)
    for r in rows:
        monday = r.local_date - timedelta(days=r.local_date.weekday())
        week[monday] += r.moving_s or r.elapsed_s or 0
        day_load[r.local_date] += r.training_load or 0
    top_week = max(week.items(), key=lambda kv: kv[1])
    top_day = max(day_load.items(), key=lambda kv: kv[1])
    out: dict[str, Any] = {
        **(_longest(rows) or {}),
        "most_hours_week": {"monday": top_week[0].isoformat(), "seconds": round(top_week[1])},
    }
    if top_day[1] > 0:
        out["biggest_day_load"] = {"date": top_day[0].isoformat(), "load": round(top_day[1], 1)}
    return out
