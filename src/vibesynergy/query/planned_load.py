"""The training load a planned session will have, from its steps, in the same units as
``own_load`` (an hour at threshold is 100), so the fitness forecast continues the history.

Per step: its time (a distance at the step's pace) times the intensity, squared for power and
run pace, cubed for swim pace, and for heart-rate targets the same TRIMP that counts a done
session. A zone is the middle of that zone; a step without a target counts by its kind (an
easy warm-up, a work step at endurance pace). Strength, Pilates and other sessions without
an endurance model count at the athlete's own recent load per hour for that kind of session.
"""

from __future__ import annotations

from typing import Any

from vibesynergy.query import own_load

# Middle of each heart-rate zone as a share of LTHR (Friel's five zones).
HR_ZONE_LTHR = {1: 0.75, 2: 0.85, 3: 0.92, 4: 0.97, 5: 1.03}
# Middle of each pace zone as a share of threshold speed.
PACE_ZONE = {1: 0.75, 2: 0.84, 3: 0.91, 4: 0.98, 5: 1.05}
# Intensity of a step without a target, by its kind.
UNTARGETED = {"warmup": 0.65, "cooldown": 0.6, "recovery": 0.55, "rest": 0.0, "work": 0.75}
ENDURANCE = frozenset({"ride", "run", "swim"})


def _mid(target: dict[str, Any]) -> float:
    return (float(target.get("low") or 0) + float(target.get("high") or target.get("low") or 0)) / 2


def _zones(table: dict[int, float], target: dict[str, Any]) -> float:
    low, high = int(target.get("low") or 1), int(target.get("high") or target.get("low") or 1)
    values = [table.get(z, table[5] if z > 5 else table[1]) for z in range(low, high + 1)]
    return sum(values) / len(values)


def _threshold_speed(fam: str, inputs: own_load.Inputs) -> float | None:
    if fam == "swim" and inputs.css:
        return 100 / inputs.css
    if fam == "run" and inputs.threshold_pace:
        return 1000 / inputs.threshold_pace
    return None


def step_load(step: dict[str, Any], fam: str, inputs: own_load.Inputs) -> float:
    """The load of one step (a repeat counts its steps that many times)."""
    if step.get("kind") == "repeat":
        times = int(step.get("times") or 1)
        return times * sum(step_load(s, fam, inputs) for s in step.get("steps") or [])
    target = step.get("target") or {}
    kind = target.get("type")
    if kind == "pct_ftp":
        intensity, model = _mid(target) / 100, "power"
    elif kind == "pct_lthr":
        intensity, model = _mid(target) / 100, "hr"
    elif kind == "hr_zone":
        intensity, model = _zones(HR_ZONE_LTHR, target), "hr"
    elif kind == "pace_zone":
        intensity, model = _zones(PACE_ZONE, target), "pace"
    else:
        intensity, model = UNTARGETED.get(str(step.get("kind") or "work"), 0.75), "pace"
    seconds = float(step.get("duration_s") or 0)
    speed = _threshold_speed(fam, inputs)
    if not seconds and step.get("distance_m") and speed and intensity:
        seconds = float(step["distance_m"]) / (speed * intensity)
    if seconds <= 0 or intensity <= 0:
        return 0.0
    if model == "hr":
        lthr = inputs.lthr.get(fam) or inputs.lthr.get("run")
        top = inputs.top(fam)
        if lthr and top > inputs.rest_hr:
            return own_load.hr_load(seconds, intensity * lthr, lthr, inputs.rest_hr, top)
    exponent = 3 if fam == "swim" and model == "pace" else 2
    return seconds * intensity**exponent / 3600 * 100


def planned_load(
    steps: list[dict[str, Any]],
    fam: str,
    inputs: own_load.Inputs,
    *,
    seconds: float | None = None,
    rate_per_hour: float | None = None,
) -> tuple[float, str] | None:
    """(load, how) for a planned session, or None when nothing is known to count it with.
    ``how``: steps (from its steps), rate (planned time at the athlete's own load per hour)."""
    if fam in ENDURANCE and steps:
        load = sum(step_load(s, fam, inputs) for s in steps)
        if load > 0:
            return load, "steps"
    if seconds and rate_per_hour:
        return seconds / 3600 * rate_per_hour, "rate"
    return None
