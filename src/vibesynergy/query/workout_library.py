"""The workout library: reusable endurance workouts as parametrised templates.

The code here is the **structure** of each template only: which steps in which order, the
repeats, which parameter sets a length, and how the result is rendered (``to_intervals``,
the ``[garmin]`` strength block). Every coaching value is read from the workout method in
the knowledge base when a workout is built (``workout_method``, article ``workout-method``,
#142): cadence bands, cues, % FTP / % LTHR / zones, warm-up and cool-down minutes,
distances, rests, exercises, names, purposes, phases, load, the easier alternative and the
parameters' defaults. An approved change to that article applies to the next workout, with
no code change and no deploy. ``tests/test_workout_method.py`` fails when a coaching value
is written here again.

Targets are relative only (% FTP, % LTHR, HR or pace zone; ICU-6), so a workout scales with
a new threshold. ``render`` returns the intervals.icu text the plan and the Garmin delivery
read, and the ``method`` stamp: which version of the method built it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from vibesynergy.query import workout_method
from vibesynergy.query.workout_method import Method, Values
from vibesynergy.query.workout_model import (
    SWIM_S_PER_M,
    Workout,
    to_intervals,
    total_metres,
    total_seconds,
)

Steps = list[dict[str, Any]]


def _repeat(times: int, steps: Steps) -> dict:
    return {"kind": "repeat", "times": times, "steps": steps}


@dataclass(frozen=True)
class Template:
    id: str
    sport: str  # run, ride, swim, strength
    build: Callable[..., Steps]  # (values, **params) -> steps
    # Strength: the [garmin] exercise lines instead of steps (garmin_strength syntax).
    garmin: Callable[..., list[str]] | None = None


# --- structure per template ------------------------------------------------------------------


def _one_step(v: Values, minutes: float) -> Steps:
    return [v.step("work", minutes=minutes)]


def _run_long(v: Values, minutes: float) -> Steps:
    return [v.step("warmup"), v.step("work", minutes=minutes - v.minutes("warmup"))]


def _run_intervals(
    v: Values, reps: int, work_min: float, rest_min: float, low: float, high: float
) -> Steps:
    return [
        v.step("warmup"),
        _repeat(
            reps,
            [
                v.step("work", minutes=work_min, bounds=(low, high)),
                v.step("recovery", minutes=rest_min),
            ],
        ),
        v.step("cooldown"),
    ]


def _test(v: Values, minutes: float) -> Steps:
    return [v.step("warmup"), v.step("work", minutes=minutes), v.step("cooldown")]


def _ride_endurance(v: Values, minutes: float) -> Steps:
    main = minutes - v.minutes("warmup") - v.minutes("cooldown")
    return [v.step("warmup"), v.step("work", minutes=main), v.step("cooldown")]


def _ride_brick(v: Values, minutes: float) -> Steps:
    """The ride of a brick (BRICK-4): a warm-up, then the main set to the end and no
    cool-down, so the run starts straight off the bike's effort."""
    return [v.step("warmup"), v.step("work", minutes=minutes - v.minutes("warmup"))]


def _ride_force(v: Values, reps: int, work_min: float, minutes: float) -> Steps:
    main = reps * (work_min + v.minutes("recovery"))
    cool = max(v.floor("cooldown"), minutes - v.minutes("warmup") - main)
    return [
        v.step("warmup"),
        _repeat(reps, [v.step("work", minutes=work_min), v.step("recovery")]),
        v.step("cooldown", minutes=cool),
    ]


def _ride_sweetspot(v: Values, reps: int, work_min: float) -> Steps:
    return [
        v.step("warmup"),
        _repeat(reps, [v.step("work", minutes=work_min), v.step("recovery")]),
        v.step("cooldown"),
    ]


def _ride_ftp_test(v: Values, minutes: float) -> Steps:
    return [
        v.step("warmup"),
        v.step("opener", "work"),
        v.step("recovery"),
        v.step("work", minutes=minutes),
        v.step("cooldown"),
    ]


def _swim_technique(v: Values, drills: int, sets: int, week: int) -> Steps:
    """Technique with this ISO week's drill focus (swim_drills): each drill cue
    ``cue_repeat`` times in a row, by name."""
    from vibesynergy.query import swim_drills

    focus = swim_drills.focus_for_week(int(week))
    cues = swim_drills.drill_cues(focus)
    each = v.entry("drill").get("cue_repeat") or 1
    pairs = []
    for i in range(int(drills)):
        pairs += [
            v.step("drill", "work", cue=cues[(i // each) % len(cues)]),
            v.step("drill_rest", "rest"),
        ]
    return [
        v.step("warmup"),
        *pairs,
        _repeat(
            sets,
            [
                v.step("hundred", "work", cue=swim_drills.hundred_cue(focus)),
                v.step("hundred_rest", "rest"),
            ],
        ),
        v.step("cooldown"),
    ]


def _swim_endurance(v: Values, reps: int, metres: int) -> Steps:
    return [
        v.step("warmup"),
        _repeat(reps, [v.step("work", metres=metres), v.step("rest")]),
        v.step("cooldown"),
    ]


def _swim_css_test(v: Values) -> Steps:
    return [
        v.step("warmup"),
        v.step("long_effort", "work"),
        v.step("rest"),
        v.step("short_effort", "work"),
        v.step("cooldown"),
    ]


def _strength(v: Values, **params: Any) -> list[str]:
    """The [garmin] lines: the warm-up, then each exercise of the method in order."""
    warmup = v.entry("warmup")
    lines = [f'warmup time={warmup.get("minutes", 0):g}m "{warmup.get("cue", "")}"']
    for ex in v.get("exercises") or []:
        sets = params[ex["sets"]] if isinstance(ex["sets"], str) else ex["sets"]
        if "reps" in ex:
            reps = params[ex["reps"]] if isinstance(ex["reps"], str) else ex["reps"]
            if "reps_max" in ex:
                reps = min(reps, ex["reps_max"])
            amount = f"reps={reps}"
        else:
            amount = f"time={ex['time']}"
        side = f" side={ex['side']}" if ex.get("side") else ""
        lines.append(
            f"{ex['code']} sets={sets} {amount}{side} rest={ex.get('rest_s', 0):g}s "
            f'"{ex.get("label", "")}"'
        )
    return lines


TEMPLATES: tuple[Template, ...] = (
    Template("run_recovery", "run", _one_step),
    Template("run_easy", "run", _one_step),
    Template("run_long", "run", _run_long),
    Template("run_brick", "run", _one_step),
    Template("run_intervals", "run", _run_intervals),
    Template("run_test", "run", _test),
    Template("ride_endurance", "ride", _ride_endurance),
    Template("ride_brick", "ride", _ride_brick),
    Template("ride_force", "ride", _ride_force),
    Template("ride_sweetspot", "ride", _ride_sweetspot),
    Template("ride_ftp_test", "ride", _ride_ftp_test),
    Template("mtb", "ride", _one_step),
    Template("swim_technique", "swim", _swim_technique),
    Template("swim_endurance", "swim", _swim_endurance),
    Template("swim_css_test", "swim", _swim_css_test),
    *(
        Template(f"strength_{name}", "strength", lambda v, **_: [], garmin=_strength)
        for name in ("aa", "mt_a", "mt_b", "ms", "sm")
    ),
)
BY_ID = {template.id: template for template in TEMPLATES}
_ICU_TYPE = {"run": "Run", "ride": "Ride", "swim": "Swim", "strength": "WeightTraining"}


def _method(method: Method | None) -> Method:
    return method if method is not None else workout_method.load(None)


def defaults(template_id: str, method: Method | None = None) -> dict[str, Any]:
    """A template's parameters with the method's defaults."""
    return dict(_method(method).values(template_id).get("params") or {})


def _name(v: Values, lang: str, values: dict[str, Any]) -> str:
    text = v.text("name", lang) or v.id
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


def catalogue(lang: str = "nl", method: Method | None = None) -> list[dict[str, Any]]:
    """Every template with what it is for, its phases, load and parameters (from the
    method), and which version of the method that is."""
    method = _method(method)
    out = []
    for t in TEMPLATES:
        v = Values(t.id, method.values(t.id))
        params = dict(v.get("params") or {})
        out.append(
            {
                "id": t.id,
                "sport": t.sport,
                "name": _name(v, lang, params),
                "purpose": v.text("purpose", lang),
                "phases": list(v.get("phases") or []),
                "load": v.get("load"),
                "params": params,
                "easier": v.get("easier") or None,
                "kb_rules": list(v.get("kb_rules") or []),
            }
        )
    return out


def render(
    template_id: str, lang: str = "nl", method: Method | None = None, **params: Any
) -> dict[str, Any]:
    """One workout from a template and the method: name, intervals.icu text, planned
    seconds and the ``method`` stamp (article, version, values taken from the seed)."""
    template = BY_ID.get(template_id)
    if template is None:
        raise ValueError(f"unknown template {template_id!r}; see the catalogue")
    if lang not in ("nl", "en"):
        raise ValueError("lang is nl or en")
    method = _method(method)
    v = Values(template.id, method.values(template.id))
    known = dict(v.get("params") or {})
    unknown = set(params) - set(known)
    if unknown:
        raise ValueError(f"{template_id} takes {', '.join(known) or 'no parameters'}")
    values = {**known, **params}
    for key, value in values.items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{key} must be a positive number")
        if isinstance(value, float) and value.is_integer():
            values[key] = int(value)  # 75.0 over MCP is 75: "Duurrit Z2 75'"
    base = {
        "template": template.id,
        "sport": template.sport,
        "name": _name(v, lang, values),
        "type": _ICU_TYPE[template.sport],
        "load": v.get("load"),
        "easier": v.get("easier") or None,
        "params": values,
        "method": method.stamp(template.id),
    }
    if template.garmin is not None:
        lines = template.garmin(v, **values)
        text = "\n\n".join(
            [
                v.text("cue", lang),
                "[garmin]\n" + "\n".join(lines),
                f"- {values['minutes']}m free",
            ]
        )
        return {
            **base,
            "text": text,
            "seconds": int(timedelta(minutes=values["minutes"]).total_seconds()),
            "note": "No weights: add weight= per exercise from the athlete's history.",
        }
    steps = template.build(v, **values)
    intro = v.text("cue", lang)
    if template.id == "swim_technique":  # the week's drill focus explained
        from vibesynergy.query import swim_drills

        intro = swim_drills.intro(swim_drills.focus_for_week(int(values["week"])), lang)
    workout = Workout(steps=steps, intro=intro)
    return {
        **base,
        "text": to_intervals(workout),
        "seconds": total_seconds(steps) + round(SWIM_S_PER_M * total_metres(steps)),
    }
