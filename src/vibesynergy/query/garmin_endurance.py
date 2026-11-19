"""Endurance workouts from VibeSynergy straight to Garmin, sport by sport.

The plan's steps (``workout_model``, parsed from the intervals.icu text) become a Garmin
workout with real step types, distances and pace targets. Garmin wants absolute targets,
so the relative zones are turned into speeds from the confirmed thresholds at build time
(ICU-6); a threshold change rebuilds the workout through the fingerprint.

Off until a sport is switched on (portal preference ``garmin_delivery``). The name carries
VibeSynergy's mark (``garmin_strength.MARK``); ``garmin_strength``
does the delivery, withdrawal and self-healing for both.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any

from vibesynergy.query import evidence, garmin_strength, thresholds, workout_model

# Indoor rides (VirtualRide) stay with intervals.icu, which also feeds Zwift.
SPORTS = ("swim", "run", "ride")
_ICU_TYPES = {"swim": ("Swim",), "run": ("Run", "TrailRun"), "ride": ("Ride", "MountainBikeRide")}
_RIDE = {"sportTypeId": 2, "sportTypeKey": "cycling"}
_THRESHOLD_SPORT = {"swim": "swim", "run": "run", "ride": "bike"}
_RUN = {"sportTypeId": 1, "sportTypeKey": "running"}
_SWIM = {"sportTypeId": 4, "sportTypeKey": "swimming"}
_MULTI = {"sportTypeId": 10, "sportTypeKey": "multi_sport"}
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


_RESTS = ("rest", "recovery")


def trim_rest_before_lap(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No timed recovery right before a step that waits for the lap button, or at the end
    (#57): the watch would count down a recovery and then wait for lap again. A repeat whose
    last step is a recovery skips it in the last round (Garmin's ``skipLastRestStep``); a
    lone recovery goes."""
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        after = steps[index + 1] if index + 1 < len(steps) else None
        lap_next = after is None or bool(after.get("until_lap"))
        if not lap_next:
            out.append(step)
        elif step["kind"] in _RESTS and not step.get("until_lap"):
            continue
        elif step["kind"] == "repeat" and step["steps"] and step["steps"][-1]["kind"] in _RESTS:
            out.append({**step, "skip_last_rest": True})  # Garmin's skipLastRestStep
        else:
            out.append(step)
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
                    **({"skipLastRestStep": True} if step.get("skip_last_rest") else {}),
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
    steps = _steps(trim_rest_before_lap(workout.steps), ("run", values))
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
    steps = _steps(trim_rest_before_lap(workout.steps), ("ride", values))
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
    steps = _steps(trim_rest_before_lap(workout.steps), css)
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


_SPORT_TYPE = {"swim": _SWIM, "ride": _RIDE, "run": _RUN}


def build_multisport(name: str, parts: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Sports in order as one multisport workout (#74): a segment each, steps numbered on, with
    the transitions between them, so the watch goes from one sport to the next without a new
    start. A swim in it is open water: no pool length."""
    segments = []
    order = 1
    for index, (sport, workout) in enumerate(parts, start=1):
        steps = copy.deepcopy(workout["workoutSegments"][0]["workoutSteps"])
        order = _number(steps, order)
        segments.append(
            {"segmentOrder": index, "sportType": dict(_SPORT_TYPE[sport]), "workoutSteps": steps}
        )
    out: dict[str, Any] = {
        "workoutName": name[:80],
        "sportType": dict(_MULTI),
        "isSessionTransitionEnabled": True,
        "workoutSegments": segments,
    }
    intro = " ".join(w["description"] for _, w in parts if w.get("description"))
    if intro:
        out["description"] = intro[:1024]
    return out


def build_brick(name: str, ride: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Ride and run as one multisport workout."""
    return build_multisport(name, [("ride", ride), ("run", run)])


def _is_brick_run(row) -> bool:
    text = f"{row.external_id or ''} {row.name or ''}".lower()
    return row.brick_part == "loop" or "brick" in text


def _combined_entry(
    out: dict[str, dict], ids: list[str], mark: garmin_strength.Marks
) -> tuple[str, dict]:
    from vibesynergy.query import multisport

    sports = tuple(out[k]["sport"] for k in ids)
    sign = garmin_strength.mark_of(mark, sports)
    room = 80 - (len(sign) + 1 if sign else 0)  # Garmin keeps 80 characters, mark included
    title = multisport.combined_title([out[k]["title"] for k in ids], room)
    name = garmin_strength.marked(title, mark, sports)
    workout = build_multisport(name, [(out[k]["sport"], out[k]["workout"]) for k in ids])
    day = out[ids[-1]]["date"]
    key = "brick:" + "+".join(ids)
    return key, {
        "event_id": key,
        "date": day,
        "name": name[:80],
        "workout": workout,
        "parts": list(ids),
        "fingerprint": evidence.digest({"name": name, "date": day.isoformat(), "workout": workout}),
    }


def _bricks(
    out: dict[str, dict],
    rows: dict[str, Any],
    mark: garmin_strength.Marks = garmin_strength.MARK,
    groups: list[list[str]] | None = None,
) -> dict[str, dict]:
    """Each planned multisport becomes one multisport entry: the sessions linked by their slot
    (``groups``, every part delivered), else a brick run with an outdoor ride on the same day."""
    bricks: dict[str, dict] = {}
    linked: set[str] = set()
    for ids in groups or []:
        linked.update(ids)
        if all(k in out for k in ids):
            key, entry = _combined_entry(out, ids, mark)
            bricks[key] = entry
    rides = [k for k, v in out.items() if v["sport"] == "ride" and k not in linked]
    for run_id, run in out.items():
        if run["sport"] != "run" or run_id in linked or not _is_brick_run(rows[run_id]):
            continue
        same_day = [k for k in rides if out[k]["date"] == run["date"]]
        if not same_day:
            continue
        # The ride marked as the brick's bike part, else the longest ride that day.
        ride_id = next(
            (k for k in same_day if rows[k].brick_part == "fiets"),
            max(same_day, key=lambda k: out[k]["seconds"]),
        )
        key, entry = _combined_entry(out, [ride_id, run_id], mark)
        bricks[key] = entry
    return bricks


def _linked_groups(engine, today: date, lookahead: int) -> list[list[str]]:
    """The planned multisports in the window, as event ids in order, whatever the sports."""
    from vibesynergy.query import multisport, plan_model

    rows = plan_model.delivery_rows(engine, today, today + timedelta(days=lookahead))
    by_day: dict[date, list[Any]] = {}
    for row in rows:
        by_day.setdefault(row.date, []).append(row)
    out = []
    for day_rows in by_day.values():
        part = lambda r: multisport.event_part(r.external_id, r.brick_part)  # noqa: E731
        for group in multisport.planned_groups(day_rows, part):
            out.append([str(day_rows[i].id) for i in group])
    return out


def desired(
    engine,
    today: date,
    sports: tuple[str, ...],
    lookahead: int,
    mark: garmin_strength.Marks = garmin_strength.MARK,
) -> dict[str, dict]:
    """Upcoming sessions of VibeSynergy's plan in the switched-on sports, by the id of
    their intervals.icu copy. The content is the plan's own (#111), not the copy's."""
    from vibesynergy.query import plan_model, threshold_push

    out: dict[str, dict] = {}
    seen: dict[str, Any] = {}
    for sport in (s for s in sports if s in SPORTS):
        values, _ids, _zones = threshold_push.plan_for(engine, _THRESHOLD_SPORT[sport])
        rows = [
            r
            for r in plan_model.delivery_rows(engine, today, today + timedelta(days=lookahead))
            if r.type in _ICU_TYPES[sport]
        ]
        for row in rows:
            parsed = workout_model.parse_intervals(row.description)
            if parsed.unparsed or not parsed.steps:
                continue  # leave it to intervals.icu's own push
            name = garmin_strength.marked(
                (row.name or "").strip() or sport.capitalize(), mark, sport
            )
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
    out.update(_bricks(out, seen, mark, _linked_groups(engine, today, lookahead)))
    return out
