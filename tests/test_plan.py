"""The training calendar: Garmin plan workouts, events and plans, planned vs done."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, CalendarSpan, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import plan
from vibesynergy.sources.garmin.mappers import map_calendar, map_training_plans
from vibesynergy.store.raw import RawRecord

TODAY = date(2026, 9, 25)
CALENDAR = {
    "calendarItems": [
        {
            "itemType": "fbtAdaptiveWorkout",
            "date": "2026-09-24",
            "title": "Base",
            "sportTypeKey": "running",
            "trainingPlanId": 46786819,
            "workoutUuid": "u-24",
        },
        {
            "itemType": "fbtAdaptiveWorkout",
            "date": "2026-09-23",
            "title": "Tempo",
            "sportTypeKey": "running",
            "trainingPlanId": 46786819,
            "workoutUuid": "u-23",
        },
        {
            "itemType": "fbtAdaptiveWorkout",
            "date": "2026-09-25",
            "title": "VO2 max",
            "sportTypeKey": "running",
            "trainingPlanId": 46786819,
            "workoutUuid": "u-25",
        },
        {
            "itemType": "workout",
            "date": "2026-09-26",
            "title": "Swim 1500m",
            "sportTypeKey": "swimming",
            "id": 17,
        },
        {
            "itemType": "event",
            "date": "2026-10-10",
            "title": "Indian Summer Ultra",
            "isRace": True,
            "primaryEvent": True,
            "location": "Oldebroek",
            "shareableEventUuid": "e-1",
        },
        {
            "itemType": "event",
            "date": "2026-10-10",
            "title": "Indian Summer Ultra",
            "isRace": True,
            "primaryEvent": True,
            "shareableEventUuid": "e-1",
        },
        {"itemType": "activity", "date": "2026-09-24", "title": "Morning Run", "id": 99},
        {"itemType": "weight", "date": "2026-09-24"},
    ]
}
PLANS = {
    "trainingPlanList": [
        {
            "trainingPlanId": 46786819,
            "name": "Indian Summer Ultra - 75 km",
            "startDate": "2026-07-06T00:00:00.0",
            "endDate": "2026-10-10T00:00:00.0",
            "trainingStatus": {"statusKey": "Scheduled"},
            "trainingSubType": {"subTypeKey": "GarminRunningCoachEventBased"},
            "trainingLevel": {"levelKey": "Intermediate"},
        },
        {
            "trainingPlanId": 1,
            "name": "Old plan",
            "startDate": "2025-01-01T00:00:00.0",
            "endDate": "2025-03-01T00:00:00.0",
            "trainingStatus": {"statusKey": "Completed"},
        },
    ]
}


def _raw(
    dataset: str, payload: dict, key: str, fetched_at: datetime = datetime(2026, 9, 25, tzinfo=UTC)
) -> RawRecord:
    return RawRecord(
        id=f"{dataset}-{key}",
        source="garmin",
        dataset=dataset,
        item_key=key,
        sha256="x",
        content_type="application/json",
        request={},
        window_start=None,
        window_end=None,
        fetched_at=fetched_at,
        supersedes=None,
        payload=json.dumps(payload).encode(),
    )


def _session(day: str, title: str, sport: str) -> dict:
    return {
        "itemType": "fbtAdaptiveWorkout",
        "date": day,
        "title": title,
        "sportTypeKey": sport,
        "trainingPlanId": 46786819,
    }


def test_mappers_keep_planned_items_once() -> None:
    records = [
        *map_calendar(_raw("scheduled_workouts", CALENDAR, "2026-09")),
        *map_calendar(_raw("scheduled_workouts", CALENDAR, "2026-10")),
        *map_calendar(_raw("scheduled_workouts", CALENDAR, "2017-06")),  # Garmin repeats the plan
    ]
    items = [r for r in records if isinstance(r, PlannedItem)]
    # each reply is complete from the day it was fetched, to the end of its month
    assert [r for r in records if isinstance(r, CalendarSpan)] == [
        CalendarSpan(date(2026, 9, 25), date(2026, 9, 30)),
        CalendarSpan(date(2026, 10, 1), date(2026, 10, 31)),
    ]
    with pytest.raises(ValueError, match="Incomplete Garmin calendar"):
        list(map_calendar(_raw("scheduled_workouts", {}, "2026-09")))
    assert [(i.kind, i.title) for i in items] == [
        ("plan_workout", "Base"),
        ("plan_workout", "Tempo"),
        ("plan_workout", "VO2 max"),
        ("workout", "Swim 1500m"),
        ("event", "Indian Summer Ultra"),
    ]  # only each month's own days; activities and weigh-ins left out; doubled event once
    race = items[-1]
    assert race.is_race and race.primary and race.extra["location"] == "Oldebroek"
    assert items[0].plan_id == "46786819"
    assert items[0].source_id == "plan_workout:46786819:2026-09-24:running"
    plans = list(map_training_plans(_raw("training_plans", PLANS, "current")))
    assert plans[0].end_date == date(2026, 10, 10) and plans[0].level == "Intermediate"


def test_plan_compares_planned_with_done(runtime) -> None:
    records = [
        *map_calendar(_raw("scheduled_workouts", CALENDAR, "2026-09")),
        *map_calendar(_raw("scheduled_workouts", CALENDAR, "2026-10")),
        *map_training_plans(_raw("training_plans", PLANS, "current")),
    ]
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="2026-09",
        payload=b"{}",
        content_type="application/json",
    )
    run = Activity(
        "99", datetime(2026, 9, 24, 7, tzinfo=UTC), date(2026, 9, 24), "running", elapsed_s=3600.0
    )
    with runtime.engine.begin() as conn:
        write_records(conn, [*records, run], source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)

    out = plan.plan(
        runtime.engine, Window(TODAY - timedelta(days=7), TODAY + timedelta(days=28)), TODAY
    )
    status = {i["title"]: i["status"] for i in out["items"]}
    assert status == {
        "Tempo": "missed",
        "Base": "done",
        "VO2 max": "today",
        "Swim 1500m": "upcoming",
        "Indian Summer Ultra": "upcoming",
    }
    assert [p["name"] for p in out["active_plans"]] == ["Indian Summer Ultra - 75 km"]
    assert [i["title"] for i in out["today"]] == ["VO2 max"]
    assert (
        out["next_event"]["days_away"] == 15 and out["races"][0]["title"] == "Indian Summer Ultra"
    )
    assert {k: out["adherence"][k] for k in ("planned", "done")} == {"planned": 2, "done": 1}
    matched = next(i for i in out["items"] if i["title"] == "Base")
    assert matched["completion_match"]["confidence"] == "heuristic"
    assert matched["write_authority"] is False


def test_a_session_the_coach_drops_disappears(runtime) -> None:
    """Garmin's adaptive coach dropped Monday's sprint and kept the core session. Its reply
    lists only today onward: what it no longer has is gone, past days stay."""
    before = {
        "calendarItems": [
            _session("2026-09-24", "Base", "running"),
            _session("2026-09-28", "Sprint", "running"),
            _session("2026-09-28", "Core stabiliteit 3", "strength_training"),
        ]
    }
    after = {"calendarItems": [_session("2026-09-28", "Core stabiliteit 3", "strength_training")]}
    for payload, fetched in (
        (before, datetime(2026, 9, 20, 6, tzinfo=UTC)),
        (after, datetime(2026, 9, 28, 6, tzinfo=UTC)),
    ):
        stored = runtime.raw.put(
            source="garmin",
            dataset="scheduled_workouts",
            item_key="2026-09",
            payload=json.dumps(payload).encode(),
            content_type="application/json",
            fetched_at=fetched,
        )
        records = map_calendar(_raw("scheduled_workouts", payload, "2026-09", fetched))
        with runtime.engine.begin() as conn:
            write_records(conn, records, source="garmin", raw_item_id=stored.id)

    today = date(2026, 9, 28)
    out = plan.plan(runtime.engine, Window(date(2026, 9, 20), date(2026, 9, 30)), today)
    assert [(i["date"], i["title"]) for i in out["items"]] == [
        ("2026-09-24", "Base"),  # Garmin left it out of the later reply, but it happened
        ("2026-09-28", "Core stabiliteit 3"),
    ]
    assert [i["title"] for i in out["today"]] == ["Core stabiliteit 3"]


@pytest.mark.parametrize(
    "planned_sports,recorded_sports,confidence",
    [
        (["running", "running"], ["running"], "ambiguous"),
        (["running"], ["running", "running"], "ambiguous"),
        (["golf"], ["golf"], "unverified"),
    ],
)
def test_completion_requires_an_unambiguous_known_sport(
    runtime, planned_sports, recorded_sports, confidence
):
    day = TODAY - timedelta(days=1)
    records = [
        PlannedItem(str(i), day, "workout", f"Session {i}", sport=sport)
        for i, sport in enumerate(planned_sports)
    ]
    records += [
        Activity(
            str(i),
            datetime(2026, 9, 24, 7 + i * 2, tzinfo=UTC),
            day,
            sport,
            elapsed_s=3600,
        )
        for i, sport in enumerate(recorded_sports)
    ]
    stored = runtime.raw.put(
        source="garmin",
        dataset="test",
        item_key="matches",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)
    out = plan.plan(runtime.engine, Window(day, TODAY), TODAY)
    assert len(out["items"]) == len(planned_sports)
    for item in out["items"]:
        assert item["status"] == "uncertain"
        assert item["done_by"] == []
        assert item["write_authority"] is False
        assert item["completion_match"]["confidence"] == confidence
    assert {k: out["adherence"][k] for k in ("planned", "done")} == {
        "planned": len(planned_sports),
        "done": 0,
    }


def test_primary_race_wins(runtime):
    from vibesynergy.query import dashboard

    today = date(2026, 10, 16)
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="races-primary",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "event:b-race",
                    date(2026, 10, 31),
                    "event",
                    title="Sallandse 30 km",
                    is_race=True,
                    primary=False,
                ),
                PlannedItem(
                    "event:a-race",
                    date(2026, 11, 28),
                    "event",
                    title="Veluwe Ultra Trail 75 km",
                    is_race=True,
                    primary=True,
                    extra={"completionTarget": {"unitType": "distance", "value": 75}},
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    phase = plan.race_phase(runtime.engine, today)
    assert phase["next_race"]["name"] == "Veluwe Ultra Trail 75 km"
    assert phase["next_race"]["days_to_go"] == 43
    assert phase["plan_phase"] == {"phase": "build", "days_left": 43 - plan.TAPER_DAYS}
    assert phase["next_race"]["source_id"] == "event:a-race"
    assert phase["next_race"]["priority"] == "A"
    assert phase["next_race"]["priority_origin"] == "derived"
    assert phase["next_race"]["is_primary"] is True
    assert phase["supporting"][0] == {
        "name": "Sallandse 30 km",
        "date": "2026-10-31",
        "days_to_go": 15,
        "source": "garmin",
        "source_id": "event:b-race",
        "priority": None,
        "priority_origin": "derived",
        "is_primary": False,
    }
    later = dashboard.dashboard(
        runtime.engine,
        date(2026, 10, 18),
        timezone="Europe/Amsterdam",
        athlete=None,
        sources=[],
    )
    assert later["next_race"]["name"] == "Veluwe Ultra Trail 75 km"
    assert later["plan_phase"]["phase"] == "build"
    assert "protect freshness" not in later["verdict"]["advice"]
    assert later["supporting"][0]["name"] == "Sallandse 30 km"


def test_a_much_longer_run_is_changed(runtime) -> None:
    """A 13.5 km tempo matched by a 31 km run is changed, not done."""
    day = date(2026, 10, 16)
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="changed",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem("tempo", day, "plan_workout", "Tempoloop 13.5 km", sport="running"),
                Activity(
                    "long",
                    datetime(2026, 10, 16, 7, tzinfo=UTC),
                    day,
                    "running",
                    name="Long run",
                    distance_m=31000,
                    moving_s=3 * 3600,
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    out = plan.plan(runtime.engine, Window(day, day), day)
    item = next(i for i in out["items"] if i["title"] == "Tempoloop 13.5 km")
    assert item["status"] == "changed"
    assert item["done_by"][0]["distance_m"] == 31000
    assert {k: out["adherence"][k] for k in ("planned", "done")} == {"planned": 1, "done": 0}


def test_a_close_match_stays_done(runtime) -> None:
    day = date(2026, 10, 16)
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="close",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem("tempo", day, "plan_workout", "Tempoloop 13.5 km", sport="running"),
                Activity(
                    "tempo",
                    datetime(2026, 10, 16, 7, tzinfo=UTC),
                    day,
                    "running",
                    name="Tempo",
                    distance_m=14000,
                    moving_s=3600,
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    item = next(
        i
        for i in plan.plan(runtime.engine, Window(day, day), day)["items"]
        if i["title"] == "Tempoloop 13.5 km"
    )
    assert item["status"] == "done"
