"""Endurance workouts straight to Garmin: swim first."""

from __future__ import annotations

import json
from datetime import date

from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import garmin_endurance as ge
from vibesynergy.query import garmin_strength as gs
from vibesynergy.query import thresholds
from vibesynergy.query.workout_model import parse_intervals
from vibesynergy.sources.intervals_icu.mappers import map_events

SWIM = """Rustige duur.

Warmup
- Licht 300mtr Z1 Pace

Main set
3x
- Middel 300mtr Z2 Pace
- 30s intensity=rest

Cooldown
- Licht 200mtr Z1 Pace"""
TODAY = date(2026, 11, 5)


def test_swim_steps_become_garmin_steps_with_pace_targets_from_the_css() -> None:
    workout = ge.build_swim("🏊 Zwemmen", parse_intervals(SWIM), 135)
    assert workout["sportType"]["sportTypeKey"] == "swimming" and workout["poolLength"] == 25.0
    steps = workout["workoutSegments"][0]["workoutSteps"]
    warm, repeat, cool = steps
    assert warm["stepType"]["stepTypeKey"] == "warmup"
    assert (warm["endCondition"]["conditionTypeKey"], warm["endConditionValue"]) == (
        "distance",
        300.0,
    )
    # Z1 for CSS 2:15: slower than 2:54/100 m (174 s) -> up to 0.5747 m/s
    assert warm["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert warm["targetValueTwo"] == round(100 / 174, 4)
    work, rest = repeat["workoutSteps"]
    assert repeat["numberOfIterations"] == 3 and work["stepType"]["stepTypeKey"] == "interval"
    assert rest["stepType"]["stepTypeKey"] == "rest" and rest["endConditionValue"] == 30.0
    assert rest["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert cool["stepType"]["stepTypeKey"] == "cooldown"


def _swim_event(runtime) -> None:
    event = {
        "id": 4001,
        "category": "WORKOUT",
        "type": "Swim",
        "start_date_local": "2026-11-11T00:00:00",
        "name": "Zwemmen duur 40'",
        "description": SWIM,
        "updated": "2026-11-04T08:00:00Z",
    }
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps([event]).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )


def test_swim_is_delivered_only_when_switched_on(runtime) -> None:
    thresholds.set_value(runtime.engine, sport="swim", metric="css", value=135, today=TODAY)
    _swim_event(runtime)
    assert ge.desired(runtime.engine, TODAY, (), 21) == {}
    found = ge.desired(runtime.engine, TODAY, ("swim",), 21)
    (entry,) = found.values()
    assert entry["name"] == "Zwemmen duur 40'" and entry["date"] == date(2026, 11, 11)

    class Garmin:
        def __init__(self):
            self.calls = []

        def upload_workout(self, workout):
            self.calls.append(("upload", workout["workoutName"]))
            return {"workoutId": 9}

        def schedule_workout(self, workout_id, day):
            self.calls.append(("schedule", workout_id, day))
            return {"workoutScheduleId": 19}

    api = Garmin()
    assert gs.reconcile(runtime.engine, lambda: api, TODAY)["delivered"] == 0
    result = gs.reconcile(runtime.engine, lambda: api, TODAY, ("swim",))
    assert result["delivered"] == 1
    assert api.calls == [("upload", "Zwemmen duur 40'"), ("schedule", 9, "2026-11-11")]


def test_run_steps_get_heart_rate_targets_from_lthr_zones() -> None:
    run = parse_intervals(
        "Rustig.\n\nWarmup\n- 5m Z1 HR\n\nMain set\n- 30m Z2 HR\n\n3x\n- 4m 95-100% LTHR\n"
        "- 2m Z1 HR intensity=recovery\n\nCooldown\n- 5m Z1 HR"
    )
    workout = ge.build_run("🏃 Duurloop", run, {"lthr": 162, "max_hr": 179})
    assert workout["sportType"]["sportTypeKey"] == "running"
    warm, main, repeat, cool = workout["workoutSegments"][0]["workoutSteps"]
    assert (main["targetType"]["workoutTargetTypeKey"], main["targetValueOne"]) == (
        "heart.rate.zone",
        138.0,
    )
    assert main["targetValueTwo"] == 144.0 and main["stepType"]["stepTypeKey"] == "interval"
    work, easy = repeat["workoutSteps"]
    assert (work["targetValueOne"], work["targetValueTwo"]) == (154.0, 162.0)
    assert easy["stepType"]["stepTypeKey"] == "recovery" and easy["targetValueTwo"] == 137.0
    assert (
        warm["stepType"]["stepTypeKey"] == "warmup"
        and cool["stepType"]["stepTypeKey"] == "cooldown"
    )


def test_ride_steps_get_power_from_ftp_and_mtb_heart_rate() -> None:
    ride = parse_intervals(
        "Warmup\n- 15m 50-65% 90rpm\n\nMain set\n4x\n- 6m 75-85% 50-60rpm\n- 3m 55% 90rpm"
    )
    workout = ge.build_ride("🚴 Kracht op de fiets", ride, {"ftp": 265})
    assert workout["sportType"]["sportTypeKey"] == "cycling"
    warm, repeat = workout["workoutSegments"][0]["workoutSteps"]
    assert (warm["targetValueOne"], warm["targetValueTwo"]) == (132.0, 172.0)
    assert warm["targetType"]["workoutTargetTypeKey"] == "power.zone"
    work, easy = repeat["workoutSteps"]
    assert (work["targetValueOne"], work["targetValueTwo"]) == (199.0, 225.0)
    assert work["description"] == "50-60 rpm"
    assert (easy["targetValueOne"], easy["targetValueTwo"]) == (139.0, 153.0)  # 55% ±5%
    mtb = ge.build_ride("🚴 MTB", parse_intervals("- 55m Z1-Z2 HR"), {"lthr": 150, "max_hr": 179})
    (step,) = mtb["workoutSegments"][0]["workoutSteps"]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["targetValueTwo"] == 133.0


def _events(runtime, events) -> None:
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps(events).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )


def test_a_brick_becomes_one_multisport_workout_next_to_its_parts(runtime) -> None:
    thresholds.set_value(runtime.engine, sport="bike", metric="ftp", value=265, today=TODAY)
    thresholds.set_value(runtime.engine, sport="run", metric="lthr", value=162, today=TODAY)
    base = {"category": "WORKOUT", "start_date_local": "2026-11-08T00:00:00"}
    _events(
        runtime,
        [
            {
                **base,
                "id": 1,
                "type": "Ride",
                "name": "Lange rit Z2 1u45",
                "external_id": "lake27:2026-11-07:ride",
                "description": "Gelijkmatig.\n\nWarmup\n- 15m 50-65%\n\nMain set\n- 80m 62-72%",
            },
            {**base, "id": 2, "type": "Ride", "name": "Koffierit", "description": "- 30m 55%"},
            {
                **base,
                "id": 3,
                "type": "Run",
                "name": "Brickloop Z2 20'",
                "external_id": "lake27:2026-11-07:brick-run",
                "description": "- 20m Z2 HR",
            },
        ],
    )
    found = ge.desired(runtime.engine, TODAY, ("ride", "run"), 21)
    brick = found["brick:1+3"]  # the longest ride that day, not the coffee ride
    assert brick["name"] == "Lange rit Z2 1u45 + Brickloop Z2 20'" and brick["parts"] == [
        "1",
        "3",
    ]
    workout = brick["workout"]
    assert workout["sportType"]["sportTypeKey"] == "multi_sport"
    assert workout["isSessionTransitionEnabled"] is True
    ride, run = workout["workoutSegments"]
    assert (
        ride["sportType"]["sportTypeKey"] == "cycling"
        and run["sportType"]["sportTypeKey"] == "running"
    )
    assert [s["stepOrder"] for s in ride["workoutSteps"]] == [1, 2]
    assert [s["stepOrder"] for s in run["workoutSteps"]] == [3]
    assert {"1", "2", "3"} <= set(found)  # the separate copies stay (PLAN-7)
    # One sport switched on: no brick.
    assert not any(k.startswith("brick") for k in ge.desired(runtime.engine, TODAY, ("run",), 21))
