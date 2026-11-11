"""The workout library (step 1): reusable endurance workouts as parametrised templates.

Each template knows its sport, what it is for, the phases it fits, whether it is a hard or
an easy day, and an easier alternative for a day with poor recovery. ``render`` builds the
steps with relative targets only (% FTP, % LTHR, HR or pace zone; ICU-6), so a workout
scales with a new threshold, and returns the intervals.icu text the plan and the Garmin
delivery already read. The coach fills day 9 from here instead of typing steps;
the plan builder will use the same templates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vibesynergy.query.workout_model import Workout, to_intervals, total_seconds

Steps = list[dict[str, Any]]
# Swim steps are distances; the planned time assumes this pace (2:30 per 100 m).
SWIM_S_PER_M = 1.5


def _time(kind: str, minutes: float, target: dict | None = None, **extra: Any) -> dict:
    step: dict[str, Any] = {"kind": kind, "duration_s": round(minutes * 60)}
    if target:
        step["target"] = target
    step.update(extra)
    return step


def _dist(kind: str, metres: int, target: dict | None = None, **extra: Any) -> dict:
    step: dict[str, Any] = {"kind": kind, "distance_m": metres}
    if target:
        step["target"] = target
    step.update(extra)
    return step


def _ftp(low: int, high: int | None = None) -> dict:
    return {"type": "pct_ftp", "low": low, "high": high or low}


def _lthr(low: int, high: int | None = None) -> dict:
    return {"type": "pct_lthr", "low": low, "high": high or low}


def _hr(low: int, high: int | None = None) -> dict:
    return {"type": "hr_zone", "low": low, "high": high or low}


def _pace(low: int, high: int | None = None) -> dict:
    return {"type": "pace_zone", "low": low, "high": high or low}


def _repeat(times: int, steps: Steps) -> dict:
    return {"kind": "repeat", "times": times, "steps": steps}


@dataclass(frozen=True)
class Template:
    id: str
    sport: str  # run, ride, swim, strength
    names: dict[str, str]  # {"nl": …, "en": …} with {placeholders} from the params
    purpose: dict[str, str]
    phases: tuple[str, ...]  # base, build, peak, taper, recovery, test
    load: str  # easy, moderate, hard
    defaults: dict[str, Any]
    build: Callable[..., Steps]
    cue: dict[str, str] = field(default_factory=dict)
    easier: str | None = None  # the template for a day with poor recovery
    kb_rules: tuple[str, ...] = ()
    # Strength: the [garmin] exercise lines instead of steps (garmin_strength syntax).
    garmin: Callable[..., list[str]] | None = None


def _run_easy(minutes: int) -> Steps:
    return [_time("work", minutes, _hr(2))]


def _run_recovery(minutes: int) -> Steps:
    return [_time("work", minutes, _hr(1))]


def _run_long(minutes: int) -> Steps:
    return [_time("warmup", 10, _hr(1)), _time("work", minutes - 10, _hr(2))]


def _run_intervals(reps: int, work_min: float, rest_min: float, low: int, high: int) -> Steps:
    return [
        _time("warmup", 15, _hr(1, 2)),
        _repeat(
            reps, [_time("work", work_min, _lthr(low, high)), _time("recovery", rest_min, _hr(1))]
        ),
        _time("cooldown", 10, _hr(1)),
    ]


def _run_test(minutes: int) -> Steps:
    return [
        _time("warmup", 15, _hr(1, 2)),
        _time("work", minutes, cue="Volle bak, gelijkmatig"),
        _time("cooldown", 10, _hr(1)),
    ]


def _ride_endurance(minutes: int) -> Steps:
    return [
        _time("warmup", 15, _ftp(50, 65), cadence=[90, 90]),
        _time("work", minutes - 25, _ftp(62, 72), cadence=[85, 95]),
        _time("cooldown", 10, _ftp(55)),
    ]


def _ride_brick(minutes: int) -> Steps:
    """The ride of a brick (BRICK-4): a warm-up, then Z2 to the end and no
    cool-down, so the run starts straight off the bike's effort."""
    return [
        _time("warmup", 15, _ftp(50, 65), cadence=[90, 90]),
        _time("work", minutes - 15, _ftp(62, 72), cadence=[85, 95]),
    ]


def _ride_force(reps: int, work_min: float, minutes: int) -> Steps:
    main = reps * (work_min + 3)
    return [
        _time("warmup", 15, _ftp(50, 65), cadence=[90, 90]),
        _repeat(
            reps,
            [
                _time("work", work_min, _ftp(75, 85), cadence=[50, 60]),
                _time("recovery", 3, _ftp(55), cadence=[90, 90]),
            ],
        ),
        _time("cooldown", max(5, minutes - 15 - main), _ftp(50, 60), cadence=[90, 90]),
    ]


def _ride_sweetspot(reps: int, work_min: float) -> Steps:
    return [
        _time("warmup", 15, _ftp(50, 65), cadence=[90, 90]),
        _repeat(reps, [_time("work", work_min, _ftp(88, 93)), _time("recovery", 5, _ftp(55))]),
        _time("cooldown", 10, _ftp(50, 60)),
    ]


def _ride_ftp_test(minutes: int) -> Steps:
    return [
        _time("warmup", 20, _ftp(50, 70), cadence=[90, 90]),
        _time("work", 5, _ftp(105, 110)),
        _time("recovery", 10, _ftp(50)),
        _time("work", minutes, cue="Volle bak, gelijkmatig"),
        _time("cooldown", 15, _ftp(50)),
    ]


def _mtb(minutes: int) -> Steps:
    return [_time("work", minutes, _hr(1, 2))]


def _swim_technique(drills: int, sets: int, week: int = 1) -> Steps:
    """Technique with this ISO week's drill focus (swim_drills): each drill twice, by name."""
    from vibesynergy.query import swim_drills

    focus = swim_drills.focus_for_week(int(week))
    cues = swim_drills.drill_cues(focus)
    pairs = []
    for i in range(int(drills)):
        pairs += [
            _dist("work", 50, _pace(2), cue=cues[(i // 2) % len(cues)]),
            _time("rest", 1 / 3),
        ]
    return [
        _dist("warmup", 300, _pace(1), cue="Licht"),
        *pairs,
        _repeat(
            sets,
            [
                _dist("work", 100, _pace(2), cue=swim_drills.hundred_cue(focus)),
                _time("rest", 1 / 3),
            ],
        ),
        _dist("cooldown", 200, _pace(1), cue="Licht"),
    ]


def _swim_endurance(reps: int, metres: int) -> Steps:
    return [
        _dist("warmup", 300, _pace(1), cue="Licht"),
        _repeat(reps, [_dist("work", metres, _pace(2), cue="Middel"), _time("rest", 1 / 2)]),
        _dist("cooldown", 200, _pace(1), cue="Licht"),
    ]


def _swim_css_test() -> Steps:
    return [
        _dist("warmup", 400, _pace(1), cue="Licht"),
        _dist("work", 400, cue="Zwaar 400 voluit"),
        _time("rest", 5),
        _dist("work", 200, cue="Zwaar 200 voluit"),
        _dist("cooldown", 200, _pace(1), cue="Licht"),
    ]


def _ex(code: str, sets: int, reps: int | None, rest_s: int, label: str, **extra: str) -> str:
    amount = f"reps={reps}" if reps else f"time={extra.pop('time')}"
    more = "".join(f" {k}={v}" for k, v in extra.items())
    return f'{code} sets={sets} {amount}{more} rest={rest_s}s "{label}"'


def _warmup() -> str:
    return 'warmup time=5m "5\' rustig fietsen of roeien"'


def _strength_aa(sets: int, reps: int, minutes: int) -> list[str]:
    return [
        _warmup(),
        _ex("SQUAT/GOBLET_SQUAT", sets, reps, 75, "Goblet squat"),
        _ex("SQUAT/ALTERNATING_BOX_DUMBBELL_STEP_UPS", sets, reps, 75, "Step-ups kniehoogte"),
        _ex("DEADLIFT/ROMANIAN_DEADLIFT", sets, reps, 75, "Roemeense deadlift"),
        _ex("HIP_RAISE/SINGLE_LEG_HIP_RAISE", 4, reps, 20, "Heupbrug op een been"),
        _ex("PULL_UP/LAT_PULLDOWN", sets, reps, 75, "Lat pulldown"),
        _ex("CALF_RAISE/STANDING_CALF_RAISE", sets, 20, 45, "Kuitheffing, gestrekte knie"),
        _ex("HIP_STABILITY/DEAD_BUG", 2, 10, 30, "Dead bug"),
        _ex("PLANK/SIDE_PLANK", 3, None, 30, "Zijplank", time="30s", side="both"),
    ]


def _strength_mt_a(sets: int, reps: int, minutes: int) -> list[str]:
    return [
        _warmup(),
        _ex("CORE/INCHWORM", 1, 6, 30, "Inchworm"),
        _ex("SQUAT/BARBELL_BACK_SQUAT", sets, reps, 120, "Back squat"),
        _ex("DEADLIFT/ROMANIAN_DEADLIFT", sets, reps, 120, "Roemeense deadlift"),
        # Third leg exercise after squat and RDL: at most 8 per leg, each leg its own step so
        # the watch counts per leg (3x10 per leg too much, 3x8 at 20 kg good).
        _ex(
            "LUNGE/BARBELL_BULGARIAN_SPLIT_SQUAT",
            sets,
            min(reps, 8),
            90,
            "Bulgaarse split squat met stang",
            side="both",
        ),
        _ex("ROW/INVERTED_ROW", sets, reps, 75, "Inverted row"),
        _ex("LEG_RAISE/HANGING_KNEE_RAISE", sets, 10, 45, "Hangend knieheffen"),
        _ex("CALF_RAISE/STANDING_BARBELL_CALF_RAISE", sets, 15, 45, "Kuitheffing staand"),
        _ex("PLANK/SIDE_PLANK", 2, None, 30, "Zijplank", time="40s", side="both"),
    ]


def _strength_mt_b(sets: int, reps: int, minutes: int) -> list[str]:
    return [
        _warmup(),
        _ex("CORE/INCHWORM", 1, 6, 30, "Inchworm"),
        # RDL, not the deadlift from the floor: easier on the lower back.
        _ex("DEADLIFT/ROMANIAN_DEADLIFT", sets, reps, 120, "Roemeense deadlift, rug neutraal"),
        _ex("HIP_RAISE/BARBELL_HIP_THRUST_WITH_BENCH", sets, 12, 90, "Hip thrust tegen bank"),
        _ex("LUNGE/WALKING_LUNGE", sets, 20, 75, "Walking lunge, 10 per been"),
        _ex("PULL_UP/LAT_PULLDOWN", sets, reps, 75, "Lat pulldown"),
        _ex("PUSH_UP/PUSH_UP", sets, 12, 60, "Push-up, romp recht"),
        _ex("CALF_RAISE/WEIGHTED_SEATED_CALF_RAISE", sets, 15, 45, "Kuitheffing zittend"),
        _ex("CORE/RUSSIAN_TWIST", 2, 20, 30, "Russian twist"),
    ]


def _strength_ms(sets: int, reps: int, minutes: int) -> list[str]:
    return [
        _warmup(),
        _ex("SQUAT/BARBELL_BACK_SQUAT", sets, reps, 180, "Back squat, zwaar"),
        _ex(
            "DEADLIFT/ROMANIAN_DEADLIFT", sets, reps, 180, "Roemeense deadlift, zwaar, rug neutraal"
        ),
        _ex("PULL_UP/LAT_PULLDOWN", sets, 8, 90, "Lat pulldown"),
        _ex("CALF_RAISE/STANDING_BARBELL_CALF_RAISE", sets, 10, 60, "Kuitheffing staand"),
        _ex("PLANK/SIDE_PLANK", 2, None, 30, "Zijplank", time="40s", side="both"),
    ]


def _strength_sm(sets: int, reps: int, minutes: int) -> list[str]:
    return [
        _warmup(),
        _ex("SQUAT/BARBELL_BACK_SQUAT", sets, reps, 120, "Back squat, onderhoud"),
        _ex("DEADLIFT/ROMANIAN_DEADLIFT", sets, reps, 120, "Roemeense deadlift"),
        _ex("HIP_RAISE/SINGLE_LEG_HIP_RAISE", 2, 12, 20, "Heupbrug op een been"),
        _ex("CALF_RAISE/STANDING_CALF_RAISE", sets, 15, 45, "Kuitheffing"),
        _ex("PLANK/SIDE_PLANK", 2, None, 30, "Zijplank", time="30s", side="both"),
    ]


def _strength(
    name: str,
    purpose_nl: str,
    purpose_en: str,
    phases: tuple[str, ...],
    load: str,
    defaults: dict[str, Any],
    build: Callable[..., list[str]],
    cue_nl: str,
    cue_en: str,
    easier: str | None = None,
) -> Template:
    return Template(
        f"strength_{name}",
        "strength",
        {
            "nl": f"Kracht {name.upper().replace('_', '-')} {{minutes}}'",
            "en": f"Strength {name.upper().replace('_', '-')} {{minutes}}'",
        },
        {"nl": purpose_nl, "en": purpose_en},
        phases,
        load,
        defaults,
        lambda **_: [],
        {"nl": cue_nl, "en": cue_en},
        easier=easier,
        kb_rules=("STR-1",),
        garmin=build,
    )


# Run names say the kind of run: Herstelloop (Z1, to recover), Korte duurloop
# (easy, under 75'), Lange duurloop (easy, 75' or more). "Duurloop" alone sounded long.
TEMPLATES: tuple[Template, ...] = (
    Template(
        "run_recovery",
        "run",
        {"nl": "Herstelloop {minutes}'", "en": "Recovery run {minutes}'"},
        {
            "nl": "Herstel na een zware dag, heel rustig.",
            "en": "Recovery after a hard day, very easy.",
        },
        ("base", "build", "peak", "recovery", "taper"),
        "easy",
        {"minutes": 25},
        _run_recovery,
        {"nl": "Heel rustig, Z1; je kunt praten.", "en": "Very easy, Z1; you can talk."},
        kb_rules=("TID-1",),
    ),
    Template(
        "run_easy",
        "run",
        {"nl": "Korte duurloop {minutes}'", "en": "Short easy run {minutes}'"},
        {"nl": "Aerobe basis, praattempo.", "en": "Aerobic base at talking pace."},
        ("base", "build", "peak", "recovery"),
        "easy",
        {"minutes": 40},
        _run_easy,
        {"nl": "Rustig, praattempo.", "en": "Easy, talking pace."},
        kb_rules=("TID-1",),
    ),
    Template(
        "run_long",
        "run",
        {"nl": "Lange duurloop {minutes}'", "en": "Long run {minutes}'"},
        {"nl": "Duurvermogen en vetverbranding.", "en": "Endurance and fat use."},
        ("base", "build", "peak"),
        "moderate",
        {"minutes": 90},
        _run_long,
        {
            "nl": "Eerste 10' rustig inlopen, daarna gelijkmatig Z2.",
            "en": "First 10' easy, then steady Z2.",
        },
        easier="run_easy",
    ),
    Template(
        "run_brick",
        "run",
        {"nl": "Brickloop Z2 {minutes}'", "en": "Brick run Z2 {minutes}'"},
        {
            "nl": "Lopen op vermoeide benen direct na de rit (PLAN-7).",
            "en": "Running off the bike (PLAN-7).",
        },
        ("build", "peak"),
        "moderate",
        {"minutes": 20},
        _run_easy,
        {
            "nl": "Direct na de rit, snel omkleden. Soepel, korte passen.",
            "en": "Straight off the bike. Short, light steps.",
        },
        easier="run_easy",
        kb_rules=("BRICK-1",),
    ),
    Template(
        "run_intervals",
        "run",
        {"nl": "Intervallen {reps}x{work_min}'", "en": "Intervals {reps}x{work_min}'"},
        {"nl": "Drempel en VO2max.", "en": "Threshold and VO2max."},
        ("build", "peak"),
        "hard",
        {"reps": 4, "work_min": 4, "rest_min": 2, "low": 95, "high": 100},
        _run_intervals,
        {
            "nl": "Alleen bij goed herstel; anders duurloop.",
            "en": "Only when recovered; otherwise an easy run.",
        },
        easier="run_easy",
        kb_rules=("TID-4", "PLAN-1"),
    ),
    Template(
        "run_test",
        "run",
        {"nl": "Looptest {minutes}'", "en": "Run test {minutes}'"},
        {
            "nl": "Drempel bepalen: gemiddelde HR van de laatste 20'.",
            "en": "Threshold: average HR of the last 20'.",
        },
        ("test",),
        "hard",
        {"minutes": 30},
        _run_test,
        {"nl": "Uitgerust starten, vlak parcours.", "en": "Start rested, flat course."},
    ),
    Template(
        "ride_endurance",
        "ride",
        {"nl": "Duurrit Z2 {minutes}'", "en": "Endurance ride Z2 {minutes}'"},
        {"nl": "Aerobe basis op de fiets.", "en": "Aerobic base on the bike."},
        ("base", "build", "peak", "recovery"),
        "easy",
        {"minutes": 60},
        _ride_endurance,
        {"nl": "Gelijkmatig, drink elk kwartier.", "en": "Steady, drink every 15 minutes."},
    ),
    Template(
        "ride_brick",
        "ride",
        {"nl": "Lange rit Z2 {minutes}' (brick)", "en": "Long ride Z2 {minutes}' (brick)"},
        {
            "nl": "De rit van een brick: Z2 tot het eind, geen cooling down (BRICK-4).",
            "en": "The ride of a brick: Z2 to the end, no cool-down (BRICK-4).",
        },
        ("base", "build", "peak"),
        "easy",
        {"minutes": 105},
        _ride_brick,
        {
            "nl": "Geen cooling down: blijf tot het eind in Z2 en ga direct door naar de "
            "brickloop. Drink elk kwartier.",
            "en": "No cool-down: stay in Z2 to the end and go straight into the brick run. "
            "Drink every 15 minutes.",
        },
    ),
    Template(
        "ride_force",
        "ride",
        {
            "nl": "Kracht op de fiets {reps}x{work_min}'",
            "en": "Force on the bike {reps}x{work_min}'",
        },
        {"nl": "Spierkracht met lage cadans.", "en": "Muscular force at low cadence."},
        ("base", "build"),
        "moderate",
        {"reps": 4, "work_min": 5, "minutes": 60},
        _ride_force,
        {
            "nl": "Lage cadans, zittend, rustig bovenlichaam.",
            "en": "Low cadence, seated, quiet upper body.",
        },
        easier="ride_endurance",
    ),
    Template(
        "ride_sweetspot",
        "ride",
        {"nl": "Sweetspot {reps}x{work_min}'", "en": "Sweet spot {reps}x{work_min}'"},
        {"nl": "Drempelvermogen opbouwen.", "en": "Building threshold power."},
        ("build", "peak"),
        "hard",
        {"reps": 3, "work_min": 12},
        _ride_sweetspot,
        {"nl": "Alleen bij goed herstel.", "en": "Only when recovered."},
        easier="ride_endurance",
        kb_rules=("TID-4", "PLAN-1"),
    ),
    Template(
        "ride_ftp_test",
        "ride",
        {"nl": "FTP-test {minutes}'", "en": "FTP test {minutes}'"},
        {"nl": "FTP = 95% van het gemiddelde vermogen.", "en": "FTP = 95% of the average power."},
        ("test",),
        "hard",
        {"minutes": 20},
        _ride_ftp_test,
        {
            "nl": "Uitgerust, liefst op Zwift of de trainer.",
            "en": "Rested, ideally on the trainer.",
        },
    ),
    Template(
        "mtb",
        "ride",
        {"nl": "MTB {minutes}'", "en": "MTB {minutes}'"},
        {"nl": "Duur en techniek in het terrein.", "en": "Endurance and skills off road."},
        ("base", "build", "peak"),
        "moderate",
        {"minutes": 75},
        _mtb,
        {"nl": "Stuur op hartslag, niet op vermogen.", "en": "Ride by heart rate, not power."},
        easier="ride_endurance",
    ),
    Template(
        "swim_technique",
        "swim",
        {"nl": "Zwemmen techniek", "en": "Swim technique"},
        {"nl": "Techniek en waterligging.", "en": "Technique and body position."},
        ("base", "build", "peak", "recovery"),
        "easy",
        {"drills": 8, "sets": 5, "week": 1},
        _swim_technique,
        {"nl": "Licht = RPE 3-4, Middel = RPE 5-6.", "en": "Easy = RPE 3-4, Moderate = RPE 5-6."},
    ),
    Template(
        "swim_endurance",
        "swim",
        {"nl": "Zwemmen duur", "en": "Swim endurance"},
        {"nl": "Aerobe zwemduur.", "en": "Aerobic swim endurance."},
        ("base", "build", "peak"),
        "moderate",
        {"reps": 4, "metres": 300},
        _swim_endurance,
        {"nl": "Middel = RPE 5-6, gelijkmatig.", "en": "Moderate = RPE 5-6, steady."},
        easier="swim_technique",
    ),
    Template(
        "swim_css_test",
        "swim",
        {"nl": "CSS-test 400/200", "en": "CSS test 400/200"},
        {
            "nl": "CSS = (400 m - 200 m) / (t400 - t200).",
            "en": "CSS = (400 m - 200 m) / (t400 - t200).",
        },
        ("test",),
        "hard",
        {},
        _swim_css_test,
        {"nl": "Beide stukken voluit, rust 5'.", "en": "Both efforts all out, 5' rest."},
    ),
)
TEMPLATES += (
    _strength(
        "aa",
        "Anatomische adaptatie: pezen en techniek.",
        "Anatomical adaptation: tendons and technique.",
        ("base",),
        "moderate",
        {"sets": 2, "reps": 15, "minutes": 40},
        _strength_aa,
        "Licht gewicht, techniek eerst, nooit tot falen.",
        "Light weights, technique first, never to failure.",
    ),
    _strength(
        "mt_a",
        "Maximale transitie, squat-focus.",
        "Maximum transition, squat focus.",
        ("base", "build"),
        "hard",
        {"sets": 3, "reps": 10, "minutes": 55},
        _strength_mt_a,
        "RPE 7-8, nog 1-3 herhalingen over, nooit tot falen.",
        "RPE 7-8, 1-3 reps in reserve, never to failure.",
        easier="strength_sm",
    ),
    _strength(
        "mt_b",
        "Maximale transitie, heup-focus met de Roemeense deadlift.",
        "Maximum transition, hip focus with the Romanian deadlift.",
        ("base", "build"),
        "hard",
        {"sets": 3, "reps": 8, "minutes": 50},
        _strength_mt_b,
        "RPE 6-7, rug neutraal, nooit tot falen.",
        "RPE 6-7, neutral back, never to failure.",
        easier="strength_sm",
    ),
    _strength(
        "ms",
        "Maximale kracht: zwaar en weinig herhalingen.",
        "Maximum strength: heavy, few reps.",
        ("build",),
        "hard",
        {"sets": 4, "reps": 5, "minutes": 50},
        _strength_ms,
        "Lange rust, perfecte techniek.",
        "Long rests, perfect technique.",
        easier="strength_sm",
    ),
    _strength(
        "sm",
        "Onderhoud van kracht in de wedstrijdfase.",
        "Strength maintenance in race season.",
        ("build", "peak"),
        "moderate",
        {"sets": 2, "reps": 8, "minutes": 35},
        _strength_sm,
        "Kort en fris, geen spierpijn.",
        "Short and fresh, no soreness.",
    ),
)
BY_ID = {template.id: template for template in TEMPLATES}


def _metres(steps: Steps) -> float:
    total = 0.0
    for step in steps:
        if step.get("kind") == "repeat":
            total += int(step.get("times") or 1) * _metres(step.get("steps") or [])
        else:
            total += float(step.get("distance_m") or 0)
    return total


def catalogue(lang: str = "nl") -> list[dict[str, Any]]:
    """Every template with what it is for, its phases, load and parameters."""
    return [
        {
            "id": t.id,
            "sport": t.sport,
            "name": t.names[lang].format(**t.defaults) if t.defaults else t.names[lang],
            "purpose": t.purpose[lang],
            "phases": list(t.phases),
            "load": t.load,
            "params": dict(t.defaults),
            "easier": t.easier,
            "kb_rules": list(t.kb_rules),
        }
        for t in TEMPLATES
    ]


def render(template_id: str, lang: str = "nl", **params: Any) -> dict[str, Any]:
    """One workout from a template: name, intervals.icu text and planned seconds."""
    template = BY_ID.get(template_id)
    if template is None:
        raise ValueError(f"unknown template {template_id!r}; see the catalogue")
    if lang not in ("nl", "en"):
        raise ValueError("lang is nl or en")
    unknown = set(params) - set(template.defaults)
    if unknown:
        raise ValueError(f"{template_id} takes {', '.join(template.defaults) or 'no parameters'}")
    values = {**template.defaults, **params}
    for key, value in values.items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{key} must be a positive number")
    if template.garmin is not None:
        lines = template.garmin(**values)
        text = "\n\n".join(
            [
                template.cue.get(lang, ""),
                "[garmin]\n" + "\n".join(lines),
                f"- {values['minutes']}m free",
            ]
        )
        return {
            "template": template.id,
            "sport": template.sport,
            "name": template.names[lang].format(**values),
            "type": "WeightTraining",
            "text": text,
            "seconds": int(values["minutes"] * 60),
            "load": template.load,
            "easier": template.easier,
            "note": "No weights: add weight= per exercise from the athlete's history.",
        }
    steps = template.build(**values)
    intro = template.cue.get(lang, "")
    if template.id == "swim_technique":  # the week's drill focus explained
        from vibesynergy.query import swim_drills

        intro = swim_drills.intro(swim_drills.focus_for_week(int(values["week"])), lang)
    workout = Workout(steps=steps, intro=intro)
    return {
        "template": template.id,
        "sport": template.sport,
        "name": template.names[lang].format(**values),
        "type": {"run": "Run", "ride": "Ride", "swim": "Swim"}[template.sport],
        "text": to_intervals(workout),
        "seconds": total_seconds(steps) + round(SWIM_S_PER_M * _metres(steps)),
        "load": template.load,
        "easier": template.easier,
    }
