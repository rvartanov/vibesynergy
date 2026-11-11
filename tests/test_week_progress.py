"""The week against the plan, per sport: target vs done, still planned, planned vs actual
per workout with the intervals.icu pairing and RPE/Feel, extras, and the season and note."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from test_plan_intervals import MONDAY, SATURDAY, _seed

from vibesynergy.derive import unify
from vibesynergy.model import Activity, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import dashboard, plan, week_progress
from vibesynergy.sources.intervals_icu.mappers import map_events

WEDNESDAY = date(2026, 11, 4)


def _more(runtime) -> None:
    """A season, a run on Wednesday you coach, an extra swim, and intervals.icu's view."""
    season = [
        {
            "id": 50,
            "category": "SEASON_START",
            "start_date_local": "2026-10-31T00:00:00",
            "name": "Example triathlon 2027",
            "updated": "2026-10-30T20:08:13.000Z",
        },
        {
            "id": 51,
            "category": "WORKOUT",
            "start_date_local": "2026-11-04T00:00:00",
            "name": "Groepstraining geven, zelf Z1 60'",
            "type": "Run",
            "external_id": "lake27:2026-11-04:run",
            "moving_time": 3600,
            "updated": "2026-10-30T20:08:13.000Z",
        },
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar-2",
        payload=json.dumps(season).encode(),
        content_type="application/json",
    )
    garmin = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="week-progress",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )
        write_records(
            conn,
            [
                Activity(
                    "coach-wed",
                    datetime(2026, 11, 4, 18, tzinfo=UTC),
                    WEDNESDAY,
                    "running",
                    name="Training geven LGS",
                    moving_s=3500.0,
                ),
                Activity(
                    "swim-tue",
                    datetime(2026, 11, 3, 10, tzinfo=UTC),
                    date(2026, 11, 3),
                    "lap_swimming",
                    name="Zwemmen",
                    moving_s=1800.0,
                    distance_m=1500.0,
                ),
            ],
            source="garmin",
            raw_item_id=garmin.id,
        )
    # intervals.icu paired Monday's long ride with plan workout 3, at 95% compliance.
    runtime.raw.put(
        source="intervals_icu",
        dataset="activities",
        item_key="2026-11",
        payload=json.dumps(
            [
                {
                    "id": "i1",
                    "external_id": "ride-mon",
                    "start_date": "2026-11-02T09:00:00Z",
                    "paired_event_id": 3,
                    "compliance": 95.2,
                    "icu_training_load": 70,
                    "icu_intensity": 66.0,
                    "icu_rpe": 4,
                    "feel": 2,
                }
            ]
        ).encode(),
        content_type="application/json",
    )
    unify.rebuild(runtime.engine)


def _progress(runtime, today=WEDNESDAY) -> dict:
    return week_progress.week_progress(runtime.engine, MONDAY, today, raw=runtime.raw)


def test_per_sport_target_done_and_still_planned(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    out = _progress(runtime)
    assert out["start"] == "2026-11-02" and out["end"] == "2026-11-08"
    assert out["days_left"] == 4
    assert out["season"]["name"] == "Example triathlon 2027"
    assert out["note"]["title"] == "Wk 1 · Instap"
    assert out["note"]["focus"] == "Rustig instappen."
    sports = {s["family"]: s for s in out["sports"]}
    assert [s["family"] for s in out["sports"]] == ["swim", "ride", "run"]
    ride = sports["ride"]
    assert ride["target_s"] == 11700 and ride["basis"] == "target"
    assert ride["done_s"] == 6000
    run = sports["run"]
    assert run["target_s"] == 9900
    # The training you gave counts: the plan workout is a training you give.
    assert run["done_s"] == 3500
    swim = sports["swim"]
    assert swim["target_s"] is None and swim["basis"] is None
    assert swim["done_s"] == 1800 and swim["done_distance_m"] == 1500
    assert swim["counts"]["extra"] == 1


def test_pairing_compliance_effort_and_extras(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    sessions = {s["title"]: s for s in _progress(runtime)["sessions"]}
    ride = sessions["Lange rit Z2 1u45"]
    assert ride["status"] == "done"
    assert ride["match"] == "paired"
    assert ride["compliance"] == 95.2
    assert ride["planned"]["duration_s"] == 6300
    assert ride["actual"]["duration_s"] == 6000
    # VibeSynergy's own load of the ride, not intervals.icu's 70 (11-11-2026).
    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import training

    own = training.own_loads(
        runtime.engine, Window(date(2026, 11, 2), date(2026, 11, 8)), [ride["actual"]["id"]]
    )
    assert ride["actual"]["load"] == own.get(ride["actual"]["id"]) != 70
    assert (ride["rpe"], ride["feel"]) == (4, 2)
    coached = sessions["Groepstraining geven, zelf Z1 60'"]
    assert coached["status"] == "done"
    assert coached["actual"]["coaching"] is True
    assert sessions["Brickloop Z2 10'"]["status"] == "missed"
    assert sessions["Zwemmen"]["status"] == "extra"
    assert sessions["Zwemmen"]["in_plan"] is False


def test_still_planned_and_next_week(runtime) -> None:
    _seed(runtime)
    out = week_progress.week_progress(runtime.engine, MONDAY, SATURDAY, raw=None)
    # Seen from the Saturday before: the whole week is still to do.
    assert out["days_left"] == 7
    ride = next(s for s in out["sports"] if s["family"] == "ride")
    assert [r["title"] for r in ride["remaining"]] == ["Lange rit Z2 1u45"]
    assert ride["remaining_s"] == 6300
    earlier = week_progress.week_progress(runtime.engine, date(2026, 10, 26), SATURDAY, raw=None)
    assert earlier["next_week"]["title"] == "Wk 1 · Instap"
    assert {t["family"]: t["time_s"] for t in earlier["next_week"]["targets"]} == {
        "ride": 11700,
        "run": 9900,
    }


def test_coaching_counts_only_for_a_coaching_plan_workout(runtime) -> None:
    raw = runtime.raw.put(
        source="garmin",
        dataset="test",
        item_key="coach-plan",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem("tempo", MONDAY, "workout", "Tempo", sport="running"),
                PlannedItem(
                    "give", WEDNESDAY, "workout", "Training geven, zelf rustig", sport="running"
                ),
                Activity(
                    "c1",
                    datetime(2026, 11, 2, 18, tzinfo=UTC),
                    MONDAY,
                    "running",
                    name="Training geven A",
                    moving_s=3600.0,
                ),
                Activity(
                    "c2",
                    datetime(2026, 11, 4, 18, tzinfo=UTC),
                    WEDNESDAY,
                    "running",
                    name="Training geven B",
                    moving_s=3600.0,
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    out = plan.plan(runtime.engine, plan.calendar_week_window(MONDAY), WEDNESDAY)
    status = {i["title"]: i["status"] for i in out["items"] if i["kind"] == "workout"}
    assert status == {"Tempo": "missed", "Training geven, zelf rustig": "done"}


def test_dashboard_carries_the_week_progress(runtime) -> None:
    _seed(runtime)
    board = dashboard.dashboard(
        runtime.engine,
        MONDAY,
        timezone=runtime.settings.timezone,
        athlete=runtime.settings.athlete,
        sources=[],
    )
    progress = board["week_progress"]
    assert progress["has_plan"] is True
    assert progress["start"] == "2026-11-02"
    assert {s["family"] for s in progress["sports"]} == {"ride", "run"}


def test_an_optional_session_is_no_debt(runtime) -> None:
    """optional workouts must not weigh on the week's goal."""
    _seed(runtime)
    _more(runtime)
    optional = [
        {
            "id": 60,
            "category": "WORKOUT",
            "start_date_local": "2026-11-03T00:00:00",
            "name": "Korte rit Z1-Z2 30' (optioneel)",
            "type": "Ride",
            "moving_time": 1800,
            "updated": "2026-10-30T20:08:13.000Z",
        }
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar-optional",
        payload=json.dumps(optional).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )
    out = _progress(runtime)
    ride = {s["family"]: s for s in out["sports"]}["ride"]
    assert ride["target_s"] == 11700 and ride["optional_s"] == 1800
    assert ride["goal_s"] == 9900  # the target without the optional ride
    session = next(s for s in out["sessions"] if s["title"].endswith("(optioneel)"))
    assert session["optional"] and session["status"] == "optional"
    assert ride["counts"]["optional"] == 1 and ride["counts"]["missed"] == 0


def test_calendar_week_totals_show_what_is_planned(runtime) -> None:
    """the calendar's week tile shows planned time and distance per discipline."""
    from vibesynergy.query import schedule

    _seed(runtime)
    out = schedule.calendar(runtime.engine, MONDAY, date(2026, 11, 8), WEDNESDAY)
    week = out["week_planned"][MONDAY.isoformat()]
    assert week["ride"]["target_s"] == 11700 and week["ride"]["time_s"] > 0
    assert all({"time_s", "distance_m", "target_s"} <= set(v) for v in week.values())


def test_planned_sessions_carry_their_workout_text(runtime) -> None:
    """a planned workout's content could not be opened anywhere."""
    from vibesynergy.query import schedule

    _seed(runtime)
    event = [
        {
            "id": 70,
            "category": "WORKOUT",
            "start_date_local": "2026-11-07T00:00:00",
            "name": "Duurloop Z2 30'",
            "type": "Run",
            "description": "Rustig.\n\n- 30m Z2",
            "moving_time": 1800,
            "updated": "2026-10-30T20:08:13.000Z",
        }
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar-text",
        payload=json.dumps(event).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )
    session = next(s for s in _progress(runtime)["sessions"] if s["title"] == "Duurloop Z2 30'")
    assert session["description"] == "Rustig.\n\n- 30m Z2"
    nov7 = date(2026, 11, 7)
    day = schedule.calendar(runtime.engine, nov7, nov7, WEDNESDAY)["days"][0]
    planned = next(p for p in day["planned"] if p["title"] == "Duurloop Z2 30'")
    assert planned["description"] == "Rustig.\n\n- 30m Z2"


def test_compliance_falls_back_to_time_done_against_time_planned() -> None:
    """every done planned workout shows its compliance."""
    assert week_progress.compliance_of(3600, 3300, 102.73) == (102.7, "intervals")
    assert week_progress.compliance_of(3600, 655) == (18.2, "time")
    assert week_progress.compliance_of(None, 655) == (None, None)
