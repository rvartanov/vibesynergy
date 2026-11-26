"""Planned steps against laps (#116): a different workout of the same sport is *changed*, the
main sets are named on both sides, and thin lap data says "not checked". The laps are
anonymised copies of real activities (distance, moving time, heart rate, speed per lap)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from vibesynergy.query import plan, plan_match

SWIM_CHANGED = [  # a pool swim: 2500 m, 68', another workout on the watch
    (448.1, 300, 120, 0.67), (52.2, 0, 110, 0.0), (239.4, 50, 120, 0.21), (2.2, 0, 110, 0.0),
    (20.7, 50, 120, 2.41), (8.4, 0, 110, 0.0), (5.8, 50, 120, 8.57), (9.9, 0, 110, 0.0),
    (85.3, 50, 120, 0.59), (3.6, 0, 110, 0.0), (96.3, 0, 110, 0.0), (68.8, 50, 120, 0.73),
    (30.1, 0, 110, 0.0), (147.0, 100, 120, 0.68), (30.2, 0, 110, 0.0), (127.1, 100, 120, 0.79),
    (30.1, 0, 110, 0.0), (154.8, 100, 120, 0.65), (30.1, 0, 110, 0.0), (18.4, 0, 110, 0.0),
    (306.1, 200, 120, 0.65), (30.1, 0, 110, 0.0), (92.4, 50, 120, 0.54), (30.1, 0, 110, 0.0),
    (294.9, 200, 120, 0.68), (30.1, 0, 110, 0.0), (102.9, 50, 120, 0.49), (30.2, 0, 110, 0.0),
    (308.4, 200, 120, 0.65), (30.1, 0, 110, 0.0), (94.5, 75, 120, 0.79), (30.1, 0, 110, 0.0),
    (322.6, 200, 120, 0.62), (30.1, 0, 110, 0.0), (107.7, 50, 120, 0.46), (30.1, 0, 110, 0.0),
    (19.7, 0, 110, 0.0), (62.1, 50, 120, 0.81), (15.1, 0, 110, 0.0), (119.4, 50, 120, 0.42),
    (30.1, 0, 110, 0.0), (61.2, 50, 120, 0.82), (15.1, 0, 110, 0.0), (123.5, 50, 120, 0.41),
    (30.1, 0, 110, 0.0), (79.5, 50, 120, 0.63), (15.2, 0, 110, 0.0), (62.0, 50, 120, 0.81),
    (2.4, 0, 110, 0.0), (110.2, 50, 120, 0.45), (3.1, 0, 110, 0.0), (2.3, 0, 110, 0.0),
    (23.8, 0, 110, 0.0), (4.4, 0, 110, 0.0), (520.3, 300, 120, 0.58), (8.5, 0, 110, 0.0),
]  # fmt: skip
SWIM_LIGHT = [  # an easy swim: the planned 3x200, two laps a length off (meetfout)
    (429.2, 300, 120, 0.7), (264.2, 200, 120, 0.76), (30.1, 0, 110, 0.0), (280.6, 200, 120, 0.71),
    (30.1, 0, 110, 0.0), (315.1, 200, 120, 0.63), (30.1, 0, 110, 0.0), (317.8, 225, 120, 0.71),
    (30.1, 0, 110, 0.0), (318.1, 250, 120, 0.79), (11.9, 0, 110, 0.0), (107.7, 0, 110, 0.0),
    (1.3, 0, 110, 0.0), (5.3, 50, 120, 9.36), (28.2, 0, 110, 0.0), (34.3, 0, 110, 0.0),
    (45.4, 50, 120, 1.1), (5.8, 0, 110, 0.0),
]  # fmt: skip
TEMPO_RUN = [  # a tempo run: 4x5' with 2' recoveries, on a day an easy run was due
    (599.0, 1632, 124, 2.72), (300.0, 965, 148, 3.22), (120.0, 305, 133, 2.54),
    (300.0, 966, 146, 3.22), (120.0, 244, 130, 2.03), (300.0, 957, 146, 3.19),
    (120.0, 265, 134, 2.2), (300.0, 914, 147, 3.05), (600.0, 1491, 135, 2.49),
    (513.0, 1335, 139, 2.6),
]  # fmt: skip
TEMPO_BLOCK = [(599, 1641, 131, 2.73), (1560, 4814, 153, 3.09), (600, 1396, 133, 2.33),
               (1653, 3973, 131, 2.4)]  # fmt: skip
EASY_RUN = [(297.0, 821, 129, 2.74), (1800.0, 5198, 143, 2.89), (300.0, 778, 136, 2.6),
            (302.0, 778, 133, 2.57)]  # fmt: skip


# The plan's steps, as plan_session.workout stores them.
def _swim(kind: str, metres: float, zone: int, cue: str) -> dict:
    target = {"type": "pace_zone", "low": zone, "high": zone}
    return {"kind": kind, "distance_m": metres, "target": target, "cue": cue}


def _timed(kind: str, seconds: int, low: int, high: int | None = None) -> dict:
    target = {"type": "hr_zone", "low": low, "high": high or low}
    return {"kind": kind, "duration_s": seconds, "target": target}


def _repeat(times: int, *steps: dict) -> dict:
    return {"kind": "repeat", "times": times, "steps": list(steps)}


REST_30 = {"kind": "rest", "duration_s": 30}
SWIM_PLAN = [  # "Zwemmen duur 40'"
    _swim("warmup", 300.0, 1, "Licht"),
    _repeat(3, _swim("work", 300.0, 2, "Middel"), REST_30),
    _swim("cooldown", 200.0, 1, "Licht"),
]
LIGHT_SWIM = [  # "Zwemmen licht 30'"
    _swim("warmup", 300.0, 1, "Licht"),
    _repeat(3, _swim("work", 200.0, 1, "Licht"), REST_30),
    _swim("cooldown", 100.0, 1, "Licht"),
]
EASY_PLAN = [  # "Korte duurloop 40'"
    _timed("warmup", 600, 1),
    _timed("work", 1500, 2),
    _timed("cooldown", 300, 1),
]
INTERVAL_PLAN = [
    _timed("warmup", 600, 1, 2),
    _repeat(4, _timed("work", 300, 4), _timed("recovery", 120, 1)),
    _timed("cooldown", 600, 1),
]


def _laps(rows, *, steps: bool = False):
    return [
        {
            "index": n,
            "seconds": seconds,
            "distance_m": float(metres),
            "hr": hr,
            "power": None,
            "speed": speed,
            "step": n if steps else None,
        }
        for n, (seconds, metres, hr, speed) in enumerate(rows, start=1)
    ]


def test_the_planned_main_set_reads_like_the_plan():
    assert plan_match.planned_text(SWIM_PLAN, "swim") == "3x300 Middel"
    assert plan_match.planned_text(EASY_PLAN, "run") == "25' Z2"
    assert plan_match.planned_text(INTERVAL_PLAN, "run") == "4x5' Z4"
    # One block without a target says no more than the title.
    assert plan_match.planned_text([{"kind": "work", "duration_s": 3600}], "strength") is None
    assert plan_match.planned_text([], "swim") is None


def test_another_swim_of_the_same_sport_is_changed():
    """A swim with 3x300 Middel planned, drills, 100s and 200+50s swum."""
    found = plan_match.compare(SWIM_PLAN, "swim", _laps(SWIM_CHANGED))
    assert found["check"] == "differs"
    assert found["planned"] == "3x300 Middel"
    # Warm-up and cool-down (the first and last 300) left out; the 75 is a lap a length off.
    assert found["done"] == "5x50 + 3x100 + 4x(200+50) + 7x50"
    item = {"title": "Zwemmen duur 40'", "extra": {"duration_s": 2190.0, "distance_m": 1400.0}}
    # 68 minutes at RPE 4 is longer at a low effort: no size deviation, but another workout.
    assert plan.deviation(item, {"moving_s": 4080.0, "distance_m": 2500.0, "rpe": 4}) is None
    assert plan.deviation(item, {"moving_s": 4080.0, "rpe": 4}, found) == {
        "kind": "structure",
        "planned": "3x300 Middel",
        "done": "5x50 + 3x100 + 4x(200+50) + 7x50",
        "done_shape": None,
    }


def test_a_few_bad_swim_laps_do_not_make_a_planned_swim_changed():
    """3x200 Licht planned; two laps counted a length long and a 50 m glitch."""
    found = plan_match.compare(LIGHT_SWIM, "swim", _laps(SWIM_LIGHT))
    assert found["check"] == "match"
    assert found["planned"] == "3x200 Licht"


def test_thin_swim_data_is_not_checked():
    one_lap = _laps([(2400.0, 1400, 120, 0.6)])
    assert plan_match.compare(SWIM_PLAN, "swim", one_lap)["check"] == "not_checked"
    assert plan_match.compare(SWIM_PLAN, "swim", None)["check"] == "not_checked"
    # The whole main set as one lap: the watch saw no rests, so nothing to judge.
    coarse = _laps([(450, 300, 110, 0.67), (1350, 900, 120, 0.67), (300, 200, 110, 0.67)])
    assert plan_match.compare(SWIM_PLAN, "swim", coarse)["check"] == "not_checked"


def test_a_tempo_run_in_place_of_an_easy_run_is_changed():
    """4x5' tempo; the plan said an easy 25' Z2."""
    found = plan_match.compare(EASY_PLAN, "run", _laps(TEMPO_RUN, steps=True))
    assert found == {"check": "differs", "planned": "25' Z2", "done": "4x5'", "done_shape": None}
    item = {"title": "Korte duurloop 40'", "extra": {"duration_s": 2400.0}}
    assert plan.deviation(item, {"moving_s": 3272, "rpe": 4}, found)["kind"] == "structure"
    # One long tempo block (30-09) is another workout too.
    block = plan_match.compare(EASY_PLAN, "run", _laps(TEMPO_BLOCK, steps=True))
    assert (block["check"], block["done"]) == ("differs", "26'")


def test_an_easy_run_as_planned_matches_and_names_no_sets():
    found = plan_match.compare(EASY_PLAN, "run", _laps(EASY_RUN, steps=True))
    assert found == {"check": "match", "planned": "25' Z2", "done": None, "done_shape": "steady"}


def test_intervals_planned_against_laps():
    found = plan_match.compare(INTERVAL_PLAN, "run", _laps(TEMPO_RUN, steps=True))
    assert (found["check"], found["done"]) == ("match", "4x5'")
    # The same plan run steady, with the watch's workout laps: another workout.
    steady = plan_match.compare(INTERVAL_PLAN, "run", _laps(EASY_RUN, steps=True))
    assert (steady["check"], steady["done_shape"]) == ("differs", "steady")
    # Auto laps of 1 km cannot show 5' reps: not checked.
    auto = _laps([(330, 1000, 135, 3.0)] * 8 + [(100, 300, 135, 3.0)])
    assert plan_match.compare(INTERVAL_PLAN, "run", auto)["check"] == "not_checked"
    # One lap for the whole run: not checked.
    assert plan_match.compare(EASY_PLAN, "run", _laps([(3881, 9114, 127, 2.3)]))["check"] == (
        "not_checked"
    )


def test_a_downhill_lap_is_not_a_hard_block():
    """Fast but with a low heart rate: the hill, not the effort."""
    laps = _laps([(600, 1600, 130, 2.7), (600, 1600, 131, 2.7), (600, 2000, 118, 3.4)] * 2)
    assert plan_match.compare(EASY_PLAN, "run", laps)["check"] == "match"


def test_no_check_for_a_training_you_give_or_a_sport_without_laps():
    coached = plan_match.compare(EASY_PLAN, "run", _laps(TEMPO_RUN), coached=True)
    assert coached["check"] is None
    strength = plan_match.compare([{"kind": "work", "duration_s": 3000}], "strength", [])
    assert strength == {"check": None, "planned": None, "done": None}


def test_the_ai_coach_gets_planned_against_done():
    item = {
        "title": "Zwemmen duur 40'",
        "sport": "swimming",
        "status": "changed",
        "deviation": {"kind": "structure", "planned": "3x300 Middel", "done": "4x50"},
        "done_by": [{"sport": "lap_swimming", "moving_s": 4080.0, "distance_m": 2500.0}],
        "comparison": {"check": "differs", "planned": "3x300 Middel", "done": "4x50", "rpe": 4},
    }
    assert plan_match.for_coach(item) == {
        "planned": "Zwemmen duur 40' — 3x300 Middel",
        "done": "lap_swimming 2500 m, 68' — 4x50 · RPE 4",
        "check": "differs",
        "deviation": {"kind": "structure", "planned": "3x300 Middel", "done": "4x50"},
    }


DAY = date(2026, 11, 19)


def test_the_plan_marks_the_other_swim_changed_end_to_end(runtime):
    """Through plan.plan and week_progress: VibeSynergy's session, the activity and its laps."""
    from vibesynergy.derive import unify
    from vibesynergy.model import Activity, ActivityLap
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import plan_model, week_progress

    plan_model.create_plan(
        runtime.engine,
        name="Test plan",
        start=date(2026, 11, 16),
        end=date(2026, 12, 31),
        reason="test",
        author="test",
        plan_id="tri27",
    )
    plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=DAY,
        name="Zwemmen duur 40'",
        sport="Swim",
        description="Rustige duur.\n\nWarmup\n- Licht 300mtr Z1 Pace\n\nMain set\n3x\n"
        "- Middel 300mtr Z2 Pace\n- 30s intensity=rest\n\nCooldown\n- Licht 200mtr Z1 Pace",
        reason="test",
        author="test",
    )
    raw = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="case-swim",
        payload=b"{}",
        content_type="application/json",
    )
    laps = [
        ActivityLap("swim1", n, elapsed_s=s, moving_s=s, distance_m=m, avg_hr=hr, avg_speed=v)
        for n, (s, m, hr, v) in enumerate(SWIM_CHANGED, start=1)
    ]
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                Activity(
                    "swim1",
                    datetime(2026, 11, 19, 10, tzinfo=UTC),
                    DAY,
                    "lap_swimming",
                    name="Zwemsessie",
                    moving_s=4080.0,
                    elapsed_s=4920.0,
                    distance_m=2500.0,
                    extra={"rpe": 40},  # RPE 4: longer at a low effort is no size deviation
                ),
                *laps,
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    out = plan.plan(runtime.engine, plan.Window(DAY, DAY), DAY)
    swim = next(i for i in out["items"] if i["title"] == "Zwemmen duur 40'")
    assert swim["status"] == "changed"
    assert swim["deviation"]["kind"] == "structure"
    assert swim["comparison"]["planned"] == "3x300 Middel"
    assert swim["comparison"]["done"] == "5x50 + 3x100 + 4x(200+50) + 7x50"
    assert "_steps" not in swim
    week = week_progress.week_progress(runtime.engine, DAY, DAY, plan=out)
    session = next(s for s in week["sessions"] if s["title"] == "Zwemmen duur 40'")
    assert session["comparison"]["check"] == "differs"
    assert session["deviation"]["kind"] == "structure"
    assert session["actual"]["sport"] == "lap_swimming"


def test_a_zwift_ride_from_intervals_icu_is_not_named_hard():
    """A ride named "Zwift - Intervals icu: Rustige rit Z2 1u" was read as intervals, so hard."""
    item = {"title": "Rustige rit Z2 1u", "extra": {"duration_s": 3600}}
    ride = {"name": "Zwift - Intervals icu: Rustige rit Z2 1u", "moving_s": 3600, "rpe": 4}
    assert plan.deviation(item, ride) is None
    assert plan.deviation(item, {**ride, "name": "Intervallen"})["kind"] == "intensity"
