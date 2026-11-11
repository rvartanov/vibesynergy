"""Endurance workouts from VibeSynergy straight to Garmin, sport by sport.

The plan's steps (``workout_model``, parsed from the intervals.icu text) become a Garmin
workout with real step types, distances and pace targets. Garmin wants absolute targets,
so the relative zones are turned into speeds from the confirmed thresholds at build time
(ICU-6); a threshold change rebuilds the workout through the fingerprint.

Off until a sport is switched on (portal preference ``garmin_delivery``). In the parallel
week the workout gets an emoji name next to intervals.icu's own copy; ``garmin_strength``
does the delivery, withdrawal and self-healing for both.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.query import evidence, thresholds, workout_model
from vibesynergy.store.schema import icu_event

# Indoor rides (VirtualRide) stay with intervals.icu, which also feeds Zwift.
SPORTS = ("swim", "run", "ride")
# During the parallel week VibeSynergy's copies had a sport emoji in front, to stand
# apart from intervals.icu's push. the athlete switched that push off on 7-11: plain names now.
NAME_PREFIX = {"swim": "", "run": "", "ride": ""}
_ICU_TYPES = {"swim": ("Swim",), "run": ("Run", "TrailRun"), "ride": ("Ride", "MountainBikeRide")}
_RIDE = {"sportTypeId": 2, "sportTypeKey": "cycling"}
_THRESHOLD_SPORT = {"swim": "swim", "run": "run", "ride": "bike"}
_RUN = {"sportTypeId": 1, "sportTypeKey": "running"}
_SWIM = {"sportTypeId": 4, "sportTypeKey": "swimming"}
_MULTI = {"sportTypeId": 10, "sportTypeKey": "multi_sport"}
# A brick (PLAN-7): one multisport workout, ride then run, next to the separate copies.
BRICK_PREFIX = ""
_POOL = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
_STEP_TYPES = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "work": (3, "interval"),
    "recovery": (4, "recovery"),
    "rest": (5, "rest"),
}
_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations"}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}
_METRE = {"unitId": 1, "unitKey": "meter", "factor": 100.0}


def swim_speeds(css: float | None, low: int, high: int) -> tuple[float, float] | None:
    """Pace zones low..high (from the CSS, s/100 m) as Garmin's speeds in m/s, slow first."""
    zones = thresholds.pace_zones("swim", css)
    if not zones:
        return None
    by = {z["zone"]: z for z in zones}
    first, last = by.get(low), by.get(high)
    if not first or not last:
        return None
    slow = first["slow"] or round(first["fast"] * 1.25)
    fast = last["fast"] or round(last["slow"] / 1.1)
    return round(100 / slow, 4), round(100 / fast, 4)


def run_bpm(
    values: dict[str, float], low: int, high: int, sport: str = "run"
) -> tuple[float, float] | None:
    """Heart-rate zones low..high (Friel on LTHR) as a bpm range for Garmin."""
    zones = {
        z["zone"]: z for z in thresholds.hr_zones(sport, values.get("lthr"), values.get("max_hr"))
    }
    first, last = zones.get(low), zones.get(high)
    if not first or not last or not last["high"]:
        return None
    bottom = first["low"] or round(last["high"] * 0.8)
    return float(bottom), float(last["high"])


def run_speeds(values: dict[str, float], low: int, high: int) -> tuple[float, float] | None:
    """Run pace zones low..high (on threshold pace, s/km) as m/s, slow first."""
    zones = {z["zone"]: z for z in thresholds.pace_zones("run", values.get("threshold_pace"))}
    first, last = zones.get(low), zones.get(high)
    if not first or not last:
        return None
    slow = first["slow"] or round(first["fast"] * 1.15)
    fast = last["fast"] or round(last["slow"] / 1.05)
    return round(1000 / slow, 4), round(1000 / fast, 4)


def _target(step: dict[str, Any], sport: str, values: dict[str, float]) -> dict[str, Any]:
    target = step.get("target") or {}
    kind = target.get("type")
    if step["kind"] in ("rest", "recovery") and sport == "swim":
        return {}
    if sport == "swim" and kind == "pace_zone":
        speeds = swim_speeds(values.get("css"), target["low"], target["high"])
        return {"targetType": dict(_PACE), "range": speeds} if speeds else {}
    if sport == "run" and kind == "hr_zone":
        bpm = run_bpm(values, target["low"], target["high"])
        return {"targetType": dict(_HR), "range": bpm} if bpm else {}
    if sport == "run" and kind == "pct_lthr" and values.get("lthr"):
        lthr = values["lthr"]
        bpm = (round(lthr * target["low"] / 100.0), round(lthr * target["high"] / 100.0))
        return {"targetType": dict(_HR), "range": (float(bpm[0]), float(bpm[1]))}
    if sport == "ride" and kind == "pct_ftp" and values.get("ftp"):
        ftp = values["ftp"]
        low, high = round(ftp * target["low"] / 100.0), round(ftp * target["high"] / 100.0)
        if low == high:  # one percentage: a band of ±5% around it, as intervals.icu sends
            low, high = round(low * 0.95), round(high * 1.05)
        return {"targetType": dict(_POWER), "range": (float(low), float(high))}
    if sport == "ride" and kind == "hr_zone":
        bpm = run_bpm(values, target["low"], target["high"], sport="bike")
        return {"targetType": dict(_HR), "range": bpm} if bpm else {}
    if sport == "run" and kind == "pace_zone":
        speeds = run_speeds(values, target["low"], target["high"])
        return {"targetType": dict(_PACE), "range": speeds} if speeds else {}
    return {}


def _step(step: dict[str, Any], css: Any) -> dict[str, Any]:
    type_id, type_key = _STEP_TYPES.get(step["kind"], _STEP_TYPES["work"])
    out: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
        "targetType": dict(_NO_TARGET),
    }
    cue = step.get("cue") or ""
    if step.get("cadence"):
        low, high = step["cadence"]
        rpm = f"{low} rpm" if low == high else f"{low}-{high} rpm"
        cue = f"{cue} {rpm}".strip()
    if cue:
        out["description"] = cue[:512]
    if step.get("until_lap"):
        out["endCondition"] = dict(_LAP)
    elif "distance_m" in step:
        out["endCondition"] = dict(_DISTANCE)
        out["endConditionValue"] = float(step["distance_m"])
        out["preferredEndConditionUnit"] = dict(_METRE)
    else:
        out["endCondition"] = dict(_TIME)
        out["endConditionValue"] = float(step.get("duration_s") or 0)
    sport, values = css if isinstance(css, tuple) else ("swim", {"css": css})
    found = _target(step, sport, values)
    if found:
        out["targetType"] = found["targetType"]
        out["targetValueOne"], out["targetValueTwo"] = found["range"]
    return out


def _steps(steps: list[dict[str, Any]], css: Any) -> list[dict[str, Any]]:
    out = []
    for step in steps:
        if step["kind"] == "repeat":
            out.append(
                {
                    "type": "RepeatGroupDTO",
                    "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                    "numberOfIterations": step["times"],
                    "smartRepeat": False,
                    "endCondition": dict(_ITERATIONS),
                    "workoutSteps": _steps(step["steps"], css),
                }
            )
        else:
            out.append(_step(step, css))
    return out


def _number(steps: list[dict], start: int = 1) -> int:
    order = start
    for step in steps:
        step["stepOrder"] = order
        order += 1
        if step["type"] == "RepeatGroupDTO":
            order = _number(step["workoutSteps"], order)
    return order


def build_run(
    name: str, workout: workout_model.Workout, values: dict[str, float]
) -> dict[str, Any]:
    steps = _steps(workout.steps, ("run", values))
    _number(steps)
    out: dict[str, Any] = {
        "workoutName": name,
        "sportType": dict(_RUN),
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(_RUN), "workoutSteps": steps}],
    }
    if workout.intro:
        out["description"] = workout.intro[:1024]
    return out


def build_ride(
    name: str, workout: workout_model.Workout, values: dict[str, float]
) -> dict[str, Any]:
    steps = _steps(workout.steps, ("ride", values))
    _number(steps)
    out: dict[str, Any] = {
        "workoutName": name,
        "sportType": dict(_RIDE),
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(_RIDE), "workoutSteps": steps}],
    }
    if workout.intro:
        out["description"] = workout.intro[:1024]
    return out


def build_swim(name: str, workout: workout_model.Workout, css: float | None) -> dict[str, Any]:
    steps = _steps(workout.steps, css)
    _number(steps)
    out: dict[str, Any] = {
        "workoutName": name,
        "sportType": dict(_SWIM),
        "poolLength": 25.0,
        "poolLengthUnit": dict(_POOL),
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(_SWIM), "workoutSteps": steps}],
    }
    if workout.intro:
        out["description"] = workout.intro[:1024]
    return out


def build_brick(name: str, ride: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Ride and run as one multisport workout: two segments, steps numbered on, with the
    transition between them, so the watch goes from bike to run without a new start."""
    ride_steps = copy.deepcopy(ride["workoutSegments"][0]["workoutSteps"])
    run_steps = copy.deepcopy(run["workoutSegments"][0]["workoutSteps"])
    _number(run_steps, _number(ride_steps))
    out: dict[str, Any] = {
        "workoutName": name[:80],
        "sportType": dict(_MULTI),
        "isSessionTransitionEnabled": True,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": dict(_RIDE), "workoutSteps": ride_steps},
            {"segmentOrder": 2, "sportType": dict(_RUN), "workoutSteps": run_steps},
        ],
    }
    intro = " ".join(w["description"] for w in (ride, run) if w.get("description"))
    if intro:
        out["description"] = intro[:1024]
    return out


def _is_brick_run(row) -> bool:
    text = f"{row.external_id or ''} {row.name or ''}".lower()
    return row.brick_part == "loop" or "brick" in text


def _bricks(out: dict[str, dict], rows: dict[str, Any]) -> dict[str, dict]:
    """Each brick run with an outdoor ride on the same day becomes one multisport entry."""
    bricks: dict[str, dict] = {}
    rides = [k for k, v in out.items() if v["sport"] == "ride"]
    for run_id, run in out.items():
        if run["sport"] != "run" or not _is_brick_run(rows[run_id]):
            continue
        same_day = [k for k in rides if out[k]["date"] == run["date"]]
        if not same_day:
            continue
        # The ride marked as the brick's bike part, else the longest ride that day.
        ride_id = next(
            (k for k in same_day if rows[k].brick_part == "fiets"),
            max(same_day, key=lambda k: out[k]["seconds"]),
        )
        ride = out[ride_id]
        name = BRICK_PREFIX + f"{ride['title']} + {run['title']}"
        workout = build_brick(name, ride["workout"], run["workout"])
        key = f"brick:{ride_id}+{run_id}"
        bricks[key] = {
            "event_id": key,
            "date": run["date"],
            "name": name[:80],
            "workout": workout,
            "parts": [ride_id, run_id],
            "fingerprint": evidence.digest(
                {"name": name, "date": run["date"].isoformat(), "workout": workout}
            ),
        }
    return bricks


def desired(engine, today: date, sports: tuple[str, ...], lookahead: int) -> dict[str, dict]:
    """Upcoming plan workouts of the switched-on sports, by intervals.icu event id."""
    from vibesynergy.query import threshold_push

    out: dict[str, dict] = {}
    seen: dict[str, Any] = {}
    for sport in (s for s in sports if s in SPORTS):
        values, _ids, _zones = threshold_push.plan_for(engine, _THRESHOLD_SPORT[sport])
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    icu_event.c.id,
                    icu_event.c.date,
                    icu_event.c.name,
                    icu_event.c.description,
                    icu_event.c.external_id,
                    icu_event.c.brick_part,
                )
                .where(
                    icu_event.c.category == "WORKOUT",
                    icu_event.c.type.in_(_ICU_TYPES[sport]),
                    icu_event.c.date >= today,
                    icu_event.c.date <= today + timedelta(days=lookahead),
                )
                .order_by(icu_event.c.date)
            ).all()
        for row in rows:
            parsed = workout_model.parse_intervals(row.description)
            if parsed.unparsed or not parsed.steps:
                continue  # leave it to intervals.icu's own push
            name = NAME_PREFIX[sport] + ((row.name or "").strip() or sport.capitalize())
            if sport == "swim":
                workout = build_swim(name, parsed, values.get("css"))
            elif sport == "ride":
                workout = build_ride(name, parsed, values)
            else:
                workout = build_run(name, parsed, values)
            seen[str(row.id)] = row
            out[str(row.id)] = {
                "event_id": str(row.id),
                "date": row.date,
                "name": name,
                "title": (row.name or "").strip() or sport.capitalize(),
                "sport": sport,
                "seconds": workout_model.total_seconds(parsed.steps),
                "workout": workout,
                "fingerprint": evidence.digest(
                    {"name": name, "date": row.date.isoformat(), "workout": workout}
                ),
            }
    if "ride" in sports and "run" in sports:
        out.update(_bricks(out, seen))
    return out
