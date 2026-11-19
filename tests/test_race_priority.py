"""Races and their priority are VibeSynergy's own (#111); the primary race is the next A
race (#120). A calendar sync never changes them, and Garmin or intervals.icu events are no
races."""

from __future__ import annotations

import json
from datetime import date, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from tests_helpers import add_race

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.mcp.server import build_server
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import plan, races
from vibesynergy.query.plan import calendar_races
from vibesynergy.query.race_priority import set_priority


def _write(runtime, records, *, key: str, source: str = "garmin") -> str:
    stored = runtime.raw.put(
        source=source,
        dataset="scheduled_workouts",
        item_key=key,
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)
    return stored.id


def _seed(runtime) -> tuple[date, str, str]:
    today = today_in(runtime.settings.timezone)
    tune = add_race(runtime.engine, "Tune-up", today + timedelta(days=20))
    main = add_race(
        runtime.engine,
        "Main race",
        today + timedelta(days=80),
        "A",
        links={"garmin": "event:main"},
    )
    return today, tune, main


def test_races_come_from_vibesynergy_with_their_own_priority(runtime) -> None:
    today, tune, main = _seed(runtime)
    listed = calendar_races(runtime.engine, today)
    found = {(row["source"], row["source_id"], row["priority"]) for row in listed}
    assert found == {("vibesynergy", tune, None), ("vibesynergy", main, "A")}


def test_a_garmin_race_is_no_race_and_a_sync_changes_nothing(runtime) -> None:
    today, tune, main = _seed(runtime)
    set_priority(runtime.engine, "vibesynergy", tune, "B")
    race_day = today + timedelta(days=80)
    # Garmin's copy of the main race, now primary and moved, plus a race only on Garmin.
    _write(
        runtime,
        [
            PlannedItem("event:main", race_day, "event", title="Main", is_race=True, primary=True),
            PlannedItem(
                "event:other", today + timedelta(days=5), "event", title="Garmin only", is_race=True
            ),
            CalendarSpan(today, race_day),
        ],
        key="races",
    )
    found = {row["source_id"]: row["priority"] for row in calendar_races(runtime.engine, today)}
    assert found == {tune: "B", main: "A"}
    out = plan.plan(runtime.engine, Window(today, race_day), today)
    assert [r["title"] for r in out["races"]] == ["Tune-up", "Main race"]
    events = [i for i in out["items"] if i["kind"] == "event"]
    assert [i["source"] for i in events] == ["vibesynergy", "vibesynergy"]
    assert events[1]["extra"]["garmin"]["source_id"] == "event:main"
    # The race only on Garmin is not in the plan and never reaches the race phase.
    assert [i["source_id"] for i in out["not_in_plan"]] == ["event:other"]
    assert out["next_race"]["name"] == "Main race"

    _write(runtime, [CalendarSpan(today, race_day)], key="races-gone")
    assert {row["source_id"] for row in calendar_races(runtime.engine, today)} == {tune, main}


def test_priority_rejects_unknown_races_and_letters(runtime) -> None:
    _today, tune, main = _seed(runtime)
    with pytest.raises(ValueError):
        set_priority(runtime.engine, "vibesynergy", tune, "D")
    with pytest.raises(LookupError):
        set_priority(runtime.engine, "vibesynergy", "no-such-race", "A")
    with pytest.raises(LookupError):
        set_priority(runtime.engine, "garmin", "event:unlinked", "A")
    # The id of a race's Garmin copy finds the race.
    via_copy = set_priority(runtime.engine, "garmin", "event:main", "B")
    assert via_copy == {**via_copy, "source": "vibesynergy", "source_id": main, "priority": "B"}
    cleared = set_priority(runtime.engine, "vibesynergy", main, None)
    assert cleared["priority"] is None


def test_portal_lists_races_and_saves_priority(runtime) -> None:
    today, tune, main = _seed(runtime)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    listed = client.get("/api/calendar")
    assert listed.status_code == 200
    found = {row["source_id"]: row for row in listed.json()["data"]["races"]}
    assert found[tune]["priority"] is None and found[tune]["editable"] is True
    assert found[main]["priority"] == "A"
    saved = client.post(
        "/api/race-priority",
        json={"source": "vibesynergy", "source_id": tune, "priority": "A"},
        headers={"x-vibesynergy": "portal"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["priority"] == "A"
    again = {row["source_id"]: row["priority"] for row in calendar_races(runtime.engine, today)}
    assert again[tune] == "A"
    refused = client.post(
        "/api/race-priority",
        json={"source": "vibesynergy", "source_id": tune, "priority": "D"},
        headers={"x-vibesynergy": "portal"},
    )
    assert refused.status_code == 422
    missing = client.post(
        "/api/race-priority",
        json={"source": "garmin", "source_id": "event:nope", "priority": "A"},
        headers={"x-vibesynergy": "portal"},
    )
    assert missing.status_code == 404


def _call(runtime, tool: str, **args) -> dict:
    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise ValueError(text)
    return result.structured_content or json.loads(text)


def test_mcp_sets_race_priority(runtime) -> None:
    _today, tune, main = _seed(runtime)
    saved = _call(
        runtime,
        "vibesynergy_race_set_priority",
        source="vibesynergy",
        source_id=tune,
        priority="C",
    )
    assert saved["data"]["priority"] == "C"
    state = _call(runtime, "vibesynergy_coach_state")
    found = {row["source_id"]: row["priority"] for row in state["data"]["races"]}
    assert found == {tune: "C", main: "A"}
    with pytest.raises(ValueError, match="race not found"):
        _call(
            runtime,
            "vibesynergy_race_set_priority",
            source="garmin",
            source_id="event:missing",
            priority="A",
        )


def test_primary_is_the_next_a_race() -> None:
    """#120: an A race that is not primary while a B race is, is no longer possible."""
    today = date(2026, 11, 19)
    rows = [
        {"id": "b", "date": date(2027, 5, 30), "priority": "B"},
        {"id": "a", "date": date(2027, 7, 3), "priority": "A"},
        {"id": "old-a", "date": date(2026, 7, 4), "priority": "A"},
    ]
    assert races.primary_id(rows, today) == "a"
    assert races.primary_id(rows, date(2027, 7, 4)) is None  # after it: none until a new A
    assert races.primary_id([r for r in rows if r["id"] == "b"], today) is None


def test_the_next_a_race_sets_the_phase_and_is_primary(runtime) -> None:
    today = today_in(runtime.settings.timezone)
    tune = add_race(runtime.engine, "Tune-up", today + timedelta(days=20), "B")
    main = add_race(runtime.engine, "Main race", today + timedelta(days=50), "A")
    out = plan.plan(runtime.engine, Window(today, today), today)
    by_id = {row["source_id"]: row for row in out["races"]}
    assert by_id[main]["primary"] is True and by_id[tune]["primary"] is False
    assert by_id[tune]["priority_origin"] == "saved"
    assert out["next_race"]["source_id"] == main
    assert out["next_race"]["is_primary"] is True
    assert [row["source_id"] for row in out["supporting"]] == [tune]
    assert out["next_event"]["source_id"] == tune  # the first race ahead

    set_priority(runtime.engine, "vibesynergy", tune, "A")
    set_priority(runtime.engine, "vibesynergy", main, None)
    shifted = plan.plan(runtime.engine, Window(today, today), today)
    assert shifted["next_race"]["source_id"] == tune
    assert shifted["next_race"]["is_primary"] is True
    assert shifted["plan_phase"] == {"phase": "build", "days_left": 6}
    assert {row["source_id"]: row["priority_origin"] for row in shifted["races"]}[main] == "unset"

    mcp = _call(runtime, "vibesynergy_plan", period="this_week")["data"]
    assert mcp["next_race"]["source_id"] == tune and mcp["next_race"]["is_primary"] is True


def test_adopt_copies_calendar_races_once_and_never_changes_them(runtime) -> None:
    today = today_in(runtime.settings.timezone)
    day = today + timedelta(days=30)
    _write(
        runtime,
        [
            PlannedItem(
                "event:g",
                day,
                "event",
                title="Example Trail 30k",
                is_race=True,
                extra={
                    "completionTarget": {"unitType": "distance", "unit": "kilometer", "value": 30}
                },
            ),
            PlannedItem("event:club", today + timedelta(days=3), "event", title="Club night"),
        ],
        key="adopt",
    )
    preview = races.adopt(runtime.engine, dry_run=True)
    assert [r["name"] for r in preview["added"]] == ["Example Trail 30k"]
    assert calendar_races(runtime.engine, today) == []  # a dry run adds nothing
    done = races.adopt(runtime.engine)
    (race,) = calendar_races(runtime.engine, today)
    assert race["name"] == "Example Trail 30k" and race["sport"] == "trail"
    assert race["target"]["value"] == 30.0
    set_priority(runtime.engine, "vibesynergy", race["source_id"], "C")
    again = races.adopt(runtime.engine)
    assert again["added"] == [] and [s["name"] for s in again["skipped"]] == ["Example Trail 30k"]
    assert calendar_races(runtime.engine, today)[0]["priority"] == "C"
    assert done["added"][0]["id"] == race["source_id"]


def test_guess_sport() -> None:
    assert races.guess_sport("Cross triatlon XXL") == "triathlon"
    assert races.guess_sport("Winter Trail 38km") == "trail"
    assert races.guess_sport("Halve marathon 2027") == "run"
    text = "3,8 km zwemmen / 180 km fietsen / 42 km lopen"
    assert races.guess_sport("Example 2027", "Other", text) == "triathlon"
    assert races.guess_sport("Something", "Ride") == "ride"
    assert races.guess_sport("Something") == "other"
