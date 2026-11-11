"""The intervals.icu calendar is the plan: pushed Garmin copies fold in, the rest is not
in the plan, weekly notes and targets reach the week, races stay one per day."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from test_mcp_today import call

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import dashboard, plan
from vibesynergy.sources.intervals_icu.mappers import map_events

SATURDAY = date(2026, 10, 31)
MONDAY = date(2026, 11, 2)


def _event(eid: int, day: str, category: str, name: str, **extra) -> dict:
    return {
        "id": eid,
        "category": category,
        "start_date_local": f"{day}T00:00:00",
        "name": name,
        "updated": "2026-10-30T20:08:13.000Z",
        **extra,
    }


EVENTS = [
    _event(
        1,
        "2026-10-31",
        "WORKOUT",
        "Duurrit Z2 60'",
        type="Ride",
        external_id="lake27:2026-10-31:ride",
        moving_time=3600,
    ),
    _event(
        2,
        "2026-11-01",
        "WORKOUT",
        "Duurloop Z2 60'",
        type="Run",
        external_id="lake27:2026-11-01:run",
        moving_time=3600,
    ),
    _event(
        3,
        "2026-11-02",
        "WORKOUT",
        "Lange rit Z2 1u45",
        type="Ride",
        external_id="lake27:2026-11-02:ride",
        moving_time=6300,
    ),
    _event(
        4,
        "2026-11-02",
        "WORKOUT",
        "Brickloop Z2 10'",
        type="Run",
        external_id="lake27:2026-11-02:brick-run",
        moving_time=600,
    ),
    _event(
        5,
        "2026-11-02",
        "NOTE",
        "Wk 1 · Instap",
        external_id="lake27:week:01",
        for_week=True,
        description="Rustig instappen.",
    ),
    _event(
        6,
        "2026-11-02",
        "TARGET",
        "Fiets 3u15",
        type="Ride",
        external_id="lake27:target:01:ride",
        for_week=True,
        time_target=11700,
    ),
    _event(
        7,
        "2026-11-02",
        "TARGET",
        "Loop 2u45",
        type="Run",
        external_id="lake27:target:01:run",
        for_week=True,
        time_target=9900,
    ),
    _event(8, "2027-07-03", "RACE_A", "EXAMPLE Triathlon", type="Other", external_id="r:a"),
    _event(9, "2026-12-20", "RACE_C", "WINTER TRAIL", type="Run", external_id="r:c"),
]


def _seed(runtime) -> None:
    runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps(EVENTS).encode(),
        content_type="application/json",
    )
    raw_id = next(runtime.raw.current_ids("intervals_icu", "events"))
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="2026-11",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(raw_id))),
            source="intervals_icu",
            raw_item_id=raw_id,
        )
        write_records(
            conn,
            [
                # Before the intervals.icu plan starts Garmin is still the plan.
                PlannedItem("workout:old", date(2026, 10, 29), "workout", "Old", sport="running"),
                # The copy intervals.icu pushed, and one that came from elsewhere.
                PlannedItem("workout:copy", SATURDAY, "workout", "Duurrit Z2 60'", sport="cycling"),
                PlannedItem(
                    "workout:foreign",
                    SATURDAY,
                    "workout",
                    "2026-10-31 Threshold Intervals",
                    sport="cycling",
                ),
                PlannedItem(
                    "workout:brick",
                    MONDAY,
                    "workout",
                    "Brick 2026-11-02 105+10 (lake27)",
                    sport="multi_sport",
                ),
                # VibeSynergy's own brick: "ride + run", no plan tag in the name.
                PlannedItem(
                    "workout:vbrick",
                    MONDAY,
                    "workout",
                    "🚴🏃 Lange rit Z2 1u45 + Brickloop Z2 10'",
                    sport="multi_sport",
                ),
                PlannedItem(
                    "event:example",
                    date(2027, 7, 3),
                    "event",
                    title="Example triathlon 2027",
                    is_race=True,
                    primary=True,
                ),
                Activity(
                    "ride-sat",
                    datetime(2026, 10, 31, 9, tzinfo=UTC),
                    SATURDAY,
                    "cycling",
                    name="Rit",
                    moving_s=3500.0,
                ),
                Activity(
                    "ride-mon",
                    datetime(2026, 11, 2, 9, tzinfo=UTC),
                    MONDAY,
                    "cycling",
                    name="Lange rit",
                    moving_s=6000.0,
                ),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)


def _sessions(out: dict) -> list[dict]:
    return [i for i in out["items"] if i["kind"] in plan.TRAINING_KINDS]


def test_pushed_copy_is_one_intervals_item_and_foreign_is_not_in_plan(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(SATURDAY, SATURDAY), SATURDAY)
    (today,) = out["today"]
    assert today["source"] == "intervals_icu"
    assert today["title"] == "Duurrit Z2 60'"
    assert today["sport"] == "cycling"
    assert today["in_plan"] is True
    assert today["ownership"] == "intervals_plan"
    assert today["extra"]["external_id"] == "lake27:2026-10-31:ride"
    assert [(c["source_id"], c["match"]) for c in today["garmin_copies"]] == [
        ("workout:copy", "name")
    ]
    assert today["status"] == "done"
    assert [i["source_id"] for i in _sessions(out)] == ["1"]
    (foreign,) = out["not_in_plan"]
    assert foreign["source_id"] == "workout:foreign"
    assert foreign["in_plan"] is False
    assert foreign["status"] == "not_in_plan"
    assert out["plan_source"] == "intervals_icu"
    assert out["plan_start"] == "2026-10-31"


def test_brick_copy_with_the_plan_tag_folds_in(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(MONDAY, MONDAY), MONDAY)
    ride = next(i for i in out["items"] if i["source_id"] == "3")
    assert sorted((c["source_id"], c["match"]) for c in ride["garmin_copies"]) == [
        ("workout:brick", "plan_tag"),
        ("workout:vbrick", "brick"),
    ]
    assert out["not_in_plan"] == []


def test_adherence_counts_the_intervals_plan(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(date(2026, 10, 26), date(2026, 11, 8)), MONDAY)
    status = {i["source_id"]: i["status"] for i in _sessions(out)}
    assert status["workout:old"] == "missed"  # Garmin, before the plan started
    assert status["1"] == "done"
    assert status["2"] == "missed"
    assert status["3"] == "done"
    assert status["4"] == "today"
    assert out["adherence"] == {
        "planned": 4,
        "done": 2,
        "changed": 0,
        "missed": 2,
        "not_in_plan": 1,
    }


def test_week_has_the_note_and_targets(runtime, monkeypatch) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, plan.calendar_week_window(MONDAY), MONDAY)
    (week,) = out["weeks"]
    assert week["start"] == "2026-11-02"
    assert week["note"]["title"] == "Wk 1 · Instap"
    assert week["note"]["text"] == "Rustig instappen."
    targets = {t["sport"]: t for t in week["targets"]}
    assert targets["cycling"]["time_s"] == 11700
    assert targets["cycling"]["done_s"] == 6000
    assert targets["running"]["done_s"] == 0
    assert week["time_s"] == 21600

    board = dashboard.dashboard(
        runtime.engine,
        MONDAY,
        timezone=runtime.settings.timezone,
        athlete=runtime.settings.athlete,
        sources=[],
    )
    assert board["week"]["compare"] == "plan"
    assert board["week"]["basis"] == "targets"
    assert board["week"]["target"]["moving_s"] == 21600
    assert {t["sport"]: t["planned_s"] for t in board["week"]["targets"]} == {
        "cycling": 11700,
        "running": 9900,
    }
    assert board["week"]["note"]["title"] == "Wk 1 · Instap"
    assert [i["title"] for i in board["plan"]["today"]] == ["Lange rit Z2 1u45", "Brickloop Z2 10'"]

    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: MONDAY)
    week_tool = call(runtime, "vibesynergy_week")["data"]
    assert week_tool["plan_source"] == "intervals_icu"
    assert week_tool["note"]["title"] == "Wk 1 · Instap"
    assert {t["sport"] for t in week_tool["targets"]} == {"cycling", "running"}
    assert {i["id"] for i in week_tool["items"]} == {
        "intervals_icu:3",
        "intervals_icu:4",
    }
    today_tool = call(runtime, "vibesynergy_today")["data"]
    assert [i["id"] for i in today_tool["today"]] == [i["id"] for i in week_tool["items"]]
    assert today_tool["not_in_plan"] == []


def test_tools_and_dashboard_agree_on_a_foreign_workout(runtime, monkeypatch) -> None:
    _seed(runtime)
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: SATURDAY)
    today_tool = call(runtime, "vibesynergy_today")["data"]
    assert today_tool["planned_today"]["id"] == "intervals_icu:1"
    assert [i["id"] for i in today_tool["not_in_plan"]] == ["garmin:workout:foreign"]
    plan_tool = call(runtime, "vibesynergy_plan", period="this_week")["data"]
    assert [i["title"] for i in plan_tool["today"]] == ["Duurrit Z2 60'"]
    assert [i["source_id"] for i in plan_tool["not_in_plan"]] == ["workout:foreign"]
    week_tool = call(runtime, "vibesynergy_week")["data"]
    assert "garmin:workout:foreign" not in {i["id"] for i in week_tool["items"]}
    assert [i["id"] for i in week_tool["not_in_plan"]] == ["garmin:workout:foreign"]
    board = dashboard.dashboard(
        runtime.engine,
        SATURDAY,
        timezone=runtime.settings.timezone,
        athlete=runtime.settings.athlete,
        sources=[],
    )
    assert [i["source"] for i in board["plan"]["today"]] == ["intervals_icu"]
    assert [i["source_id"] for i in board["plan"]["not_in_plan"]] == ["workout:foreign"]
    assert all(i.get("in_plan", True) for i in board["plan"]["week"])


def test_races_stay_one_per_day(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(SATURDAY, SATURDAY), SATURDAY)
    races = [(r["date"], r["source"], r["priority"], r["priority_origin"]) for r in out["races"]]
    assert races == [
        ("2026-12-20", "intervals_icu", "C", "intervals"),
        ("2027-07-03", "garmin", "A", "derived"),
    ]
    race = out["races"][1]
    assert race["intervals_id"] == "8"
    assert out["next_race"]["name"] == "Example triathlon 2027"
    assert [s["name"] for s in out["supporting"]] == ["WINTER TRAIL"]
    phase = plan.race_phase(runtime.engine, SATURDAY)
    assert phase["next_race"]["name"] == "Example triathlon 2027"
    # A window that holds a race formats it once for items and once for the race list.
    wide = plan.plan(runtime.engine, Window(SATURDAY, date(2027, 7, 31)), SATURDAY)
    assert [i["source"] for i in wide["items"] if i["kind"] == "event"] == [
        "intervals_icu",
        "garmin",
    ]
    assert wide["races"][0]["days_away"] == 50
    # The calendar lists both; only a Garmin race can store a priority.
    listed = plan.calendar_races(runtime.engine, SATURDAY)
    assert [(r["source"], r["editable"]) for r in listed] == [
        ("intervals_icu", False),
        ("garmin", True),
    ]


def test_no_intervals_plan_keeps_garmin(runtime) -> None:
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="2026-11",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [PlannedItem("workout:x", SATURDAY, "workout", "Easy", sport="running")],
            source="garmin",
            raw_item_id=stored.id,
        )
    out = plan.plan(runtime.engine, Window(SATURDAY, SATURDAY), SATURDAY)
    assert [i["source_id"] for i in out["today"]] == ["workout:x"]
    assert out["not_in_plan"] == []
    assert out["plan_source"] == "garmin"
    assert out["weeks"] == []


def test_training_plan_overview(runtime) -> None:
    from vibesynergy.query import training_plan

    _seed(runtime)
    out = training_plan.overview(runtime.engine, MONDAY)
    (week,) = out["weeks"]
    assert week["number"] == 1
    assert week["phase"] == "Instap"
    assert week["when"] == "current"
    assert out["current_week"] == "2026-11-02"
    assert {g["sport"]: g["done_s"] for g in week["targets"]} == {"cycling": 6000, "running": 0}
    assert out["phases"] == [
        {"phase": "Instap", "start": "2026-11-02", "end": "2026-11-08", "weeks": [1]}
    ]
    assert [r["title"] for r in out["races"]] == ["WINTER TRAIL", "Example triathlon 2027"]
    assert [i["id"] for i in out["upcoming"]["items"]] == ["intervals_icu:3", "intervals_icu:4"]


def test_week_titles_parse() -> None:
    from vibesynergy.query.training_plan import parse_title

    assert parse_title("Wk 31 · Herstel · herstelweek · ~8u") == {
        "number": 31,
        "phase": "Herstel",
        "kind": "recovery",
        "hours_label": "~8u",
    }
    assert parse_title("Wk 7 · Base 1 · raceweek · ~6u")["kind"] == "race"
    assert parse_title("Wk 34 · Taper · taperweek · ~10u")["kind"] == "taper"
    assert parse_title(None) == {"number": None, "phase": None, "kind": None, "hours_label": None}


def test_vibesynergy_emoji_copy_matches_its_plan_workout_by_name() -> None:
    """VibeSynergy's emoji-named Garmin copy belongs to the plan workout."""
    from vibesynergy.query.plan import _name_key

    assert _name_key("🏋️ Kracht AA 40'") == _name_key("Kracht AA 40'")
    assert _name_key("🧘 Pilates 60'") == _name_key("pilates  60'")
