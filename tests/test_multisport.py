"""Multisport as a holder: parts found by time, the holder counted once."""

from __future__ import annotations

from datetime import UTC, datetime

from vibesynergy.query import multisport


def _row(ident, sport, hour, minute, seconds):
    return {
        "id": ident,
        "sport": sport,
        "start_utc": datetime(2026, 11, 8, hour, minute, tzinfo=UTC),
        "elapsed_s": seconds,
    }


def test_parts_are_the_activities_inside_the_holder() -> None:
    rows = [
        _row("ride", "virtual_ride", 9, 7, 6306),
        _row("brick", "multi_sport", 9, 7, 7926),
        _row("t2", "Workout", 10, 52, 371),
        _row("run", "Run", 10, 58, 1247),
        _row("later", "Run", 15, 0, 1800),  # another run that afternoon: not a part
    ]
    assert multisport.parts(rows) == {"brick": ["ride", "t2", "run"]}
    assert multisport.parts(rows[:1] + rows[4:]) == {}  # no holder, no grouping
    assert multisport.is_multisport("triathlon") and not multisport.is_multisport("running")


def test_a_planned_brick_is_one_holder_in_the_calendar(runtime) -> None:
    import json
    from datetime import date

    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import schedule
    from vibesynergy.sources.intervals_icu.mappers import map_events

    events = [
        {
            "id": 81,
            "category": "WORKOUT",
            "start_date_local": "2026-11-15T00:00:00",
            "name": "Lange rit Z2 1u45",
            "type": "Ride",
            "moving_time": 6300,
            "updated": "2026-11-01T10:00:00.000Z",
        },
        {
            "id": 82,
            "category": "WORKOUT",
            "start_date_local": "2026-11-15T00:00:00",
            "name": "Brickloop Z2 20'",
            "type": "Run",
            "moving_time": 1200,
            "updated": "2026-11-01T10:00:00.000Z",
        },
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="brick",
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
    day = date(2026, 11, 15)
    out = schedule.calendar(runtime.engine, day, day, date(2026, 11, 10))["days"][0]
    (holder,) = out["planned_multisport"]
    assert holder["title"] == "Lange rit Z2 1u45 + Brickloop Z2 20'"
    assert holder["planned_s"] == 7500


def test_activity_list_marks_the_holder_and_counts_parts_once(runtime) -> None:
    from datetime import date

    from vibesynergy.core.timeutil import Window
    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import views

    day = date(2026, 11, 8)
    stored = runtime.raw.put(
        source="garmin", dataset="t", item_key="ms", payload=b"x", content_type="text/plain"
    )

    def act(ident, sport, hour, minute, seconds):
        start = datetime(2026, 11, 8, hour, minute, tzinfo=UTC)
        return Activity(ident, start, day, sport, name=ident, moving_s=seconds, elapsed_s=seconds)

    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                act("ride", "virtual_ride", 9, 0, 3600.0),
                act("brick", "multi_sport", 9, 1, 4800.0),
                act("run", "running", 10, 2, 1200.0),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)
    out = views.activities(runtime.engine, Window(day, day), limit=10)
    rows = {a["name"]: a for a in out["activities"]}
    assert rows["brick"]["parts"] == ["garmin:ride", "garmin:run"]
    assert rows["run"]["part_of"] == "garmin:brick"
    assert out["totals"]["count"] == 2 and out["totals"]["moving_s"] == 4800.0


def test_activity_page_links_holder_and_parts(runtime) -> None:
    from datetime import date

    from fastapi.testclient import TestClient

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.portal.app import create_portal_app

    day = date(2026, 11, 8)
    stored = runtime.raw.put(
        source="garmin", dataset="t", item_key="ms2", payload=b"y", content_type="text/plain"
    )
    start = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                Activity("brick", start, day, "multi_sport", name="Brick", elapsed_s=4800.0),
                Activity("ride", start, day, "cycling", name="Ride", moving_s=3600.0),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    holder = api.get("/api/activity", params={"id": "garmin:brick"}).json()["data"]
    assert [p["name"] for p in holder["multisport"]["parts"]] == ["Ride"]
    part = api.get("/api/activity", params={"id": "garmin:ride"}).json()["data"]
    assert part["multisport"]["holder"]["name"] == "Brick"


def test_a_brick_done_as_separate_ride_and_run_is_one_holder(runtime) -> None:
    import json
    from datetime import date

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import schedule
    from vibesynergy.sources.intervals_icu.mappers import map_events

    events = [
        {
            "id": 91,
            "category": "WORKOUT",
            "start_date_local": "2026-11-22T00:00:00",
            "name": "Lange rit Z2 1u45",
            "type": "Ride",
            "moving_time": 6300,
            "updated": "2026-11-01T10:00:00.000Z",
        },
        {
            "id": 92,
            "category": "WORKOUT",
            "start_date_local": "2026-11-22T00:00:00",
            "name": "Brickloop Z2 20'",
            "type": "Run",
            "moving_time": 1200,
            "updated": "2026-11-01T10:00:00.000Z",
        },
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="brick2",
        payload=json.dumps(events).encode(),
        content_type="application/json",
    )
    day = date(2026, 11, 22)
    garmin = runtime.raw.put(
        source="garmin", dataset="t", item_key="b2", payload=b"z", content_type="text/plain"
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
                    "r1",
                    datetime(2026, 11, 22, 9, tzinfo=UTC),
                    day,
                    "cycling",
                    name="Rit",
                    moving_s=6250.0,
                ),
                Activity(
                    "r2",
                    datetime(2026, 11, 22, 10, 50, tzinfo=UTC),
                    day,
                    "running",
                    name="Loop",
                    moving_s=1210.0,
                ),
            ],
            source="garmin",
            raw_item_id=garmin.id,
        )
    unify.rebuild(runtime.engine)
    out = schedule.calendar(runtime.engine, day, day, date(2026, 11, 23))
    entry = out["days"][0]
    (holder,) = entry["multisport"]
    assert holder["from_plan"] and holder["id"] is None
    assert holder["name"] == "Lange rit Z2 1u45 + Brickloop Z2 20'"
    assert len(holder["parts"]) == 2
    week = out["week_summary"]["2026-11-16"]
    assert week["count"] == 2  # the parts only; the holder adds nothing


def test_a_part_brings_its_holder_and_siblings_for_the_insight(runtime) -> None:
    """every part gets its own insight, the holder one over the whole."""
    from datetime import date

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records

    day = date(2026, 11, 8)
    stored = runtime.raw.put(
        source="garmin", dataset="t", item_key="ms2", payload=b"x", content_type="text/plain"
    )

    def act(ident, sport, hour, minute, seconds):
        start = datetime(2026, 11, 8, hour, minute, tzinfo=UTC)
        return Activity(ident, start, day, sport, name=ident, moving_s=seconds, elapsed_s=seconds)

    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                act("ride", "virtual_ride", 9, 0, 3600.0),
                act("brick", "multi_sport", 9, 1, 4800.0),
                act("run", "running", 10, 2, 1200.0),
                act("swim", "lap_swimming", 18, 0, 1800.0),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)
    assert multisport.relatives(runtime.engine, ["garmin:run"]) == [
        "garmin:run",
        "garmin:brick",
        "garmin:ride",
    ]
    assert multisport.relatives(runtime.engine, ["garmin:swim"]) == ["garmin:swim"]


def test_a_slot_links_a_session_into_a_planned_multisport() -> None:
    """#74: ms<n>-<sport>, an optional group letter, and the old brick slots."""
    assert multisport.slot_part("ms1-swim") == ("", 1)
    assert multisport.slot_part("ms3-run") == ("", 3)
    assert multisport.slot_part("msb2-ride") == ("b", 2)
    assert multisport.slot_part("fiets") == ("", 1) and multisport.slot_part("loop") == ("", 2)
    assert multisport.slot_part("run") is None and multisport.slot_part("brick-run") is None
    assert multisport.slot_part(None) is None
    assert multisport.event_part("fry27:2026-11-08:ms2-ride") == ("", 2)
    assert multisport.event_part("2026-11-08:ride", "fiets") == ("", 1)
    assert multisport.event_part("fry27:2026-11-08:run") is None
    items = [{"multi_part": ("", 2)}, {"multi_part": None}, {"multi_part": ("", 1)},
             {"multi_part": ("b", 1)}]  # fmt: skip
    assert multisport.planned_groups(items) == [[2, 0]]  # group b has one part: none


def test_a_planned_triathlon_is_one_holder_in_the_calendar(runtime) -> None:
    import json
    from datetime import date

    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import schedule
    from vibesynergy.sources.intervals_icu.mappers import map_events

    base = {"category": "WORKOUT", "start_date_local": "2026-11-15T00:00:00",
            "updated": "2026-11-01T10:00:00.000Z"}  # fmt: skip
    events = [
        {**base, "id": 93, "name": "Loop", "type": "Run", "moving_time": 1200,
         "external_id": "fry27:2026-11-15:ms3-run"},
        {**base, "id": 91, "name": "Zwem", "type": "Swim", "moving_time": 1800,
         "external_id": "fry27:2026-11-15:ms1-swim"},
        {**base, "id": 92, "name": "Fiets", "type": "Ride", "moving_time": 3600,
         "external_id": "fry27:2026-11-15:ms2-ride"},
    ]  # fmt: skip
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="tri",
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
    day = date(2026, 11, 15)
    out = schedule.calendar(runtime.engine, day, day, date(2026, 11, 10))["days"][0]
    (holder,) = out["planned_multisport"]
    assert holder["title"] == "Zwem + Fiets + Loop"
    assert holder["sports"] == ["swim", "ride", "run"] and holder["planned_s"] == 6600
