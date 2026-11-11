"""Race priority is stored by VibeSynergy and is not rewritten by a calendar sync."""

from __future__ import annotations

import json
from datetime import date, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import func, select

from vibesynergy.core.timeutil import today_in
from vibesynergy.mcp.server import build_server
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query.plan import calendar_races


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


def _seed(runtime):
    today = today_in(runtime.settings.timezone)
    tune = today + timedelta(days=20)
    main = today + timedelta(days=80)
    _write(
        runtime,
        [
            PlannedItem("event:tune", tune, "event", title="Tune-up", is_race=True),
            PlannedItem("event:main", main, "event", title="Main race", is_race=True, primary=True),
        ],
        key="races",
    )
    return today


def test_priority_defaults_to_unset_unless_the_race_is_garmin_primary(runtime) -> None:
    today = _seed(runtime)
    found = {(row["source_id"], row["priority"]) for row in calendar_races(runtime.engine, today)}
    assert found == {("event:tune", None), ("event:main", "A")}


def test_a_chosen_priority_survives_resync_and_a_removed_race(runtime) -> None:
    from vibesynergy.query.race_priority import set_priority
    from vibesynergy.store.schema import planned_item, race_priority

    today = _seed(runtime)
    saved = set_priority(runtime.engine, "garmin", "event:tune", "B")
    assert saved["priority"] == "B"
    main = set_priority(runtime.engine, "garmin", "event:main", "C")
    assert main["priority"] == "C"

    today = today_in(runtime.settings.timezone)
    tune = today + timedelta(days=20)
    race_day = today + timedelta(days=80)
    _write(
        runtime,
        [
            PlannedItem("event:tune", tune, "event", title="Tune-up", is_race=True),
            PlannedItem(
                "event:main", race_day, "event", title="Main race", is_race=True, primary=True
            ),
            CalendarSpan(today, race_day),
        ],
        key="races-again",
    )
    found = {row["source_id"]: row["priority"] for row in calendar_races(runtime.engine, today)}
    assert found == {"event:tune": "B", "event:main": "C"}
    with runtime.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(planned_item)).scalar() == 2

    _write(
        runtime,
        [CalendarSpan(today, race_day)],
        key="races-gone",
    )
    assert calendar_races(runtime.engine, today) == []
    with runtime.engine.connect() as conn:
        kept = conn.execute(select(func.count()).select_from(race_priority)).scalar()
    assert kept == 2

    _write(
        runtime,
        [PlannedItem("event:tune", tune, "event", title="Tune-up", is_race=True)],
        key="races-back",
    )
    back = calendar_races(runtime.engine, today)
    assert [(row["source_id"], row["priority"]) for row in back] == [("event:tune", "B")]


def test_priority_rejects_unknown_races_and_letters(runtime) -> None:
    from vibesynergy.query.race_priority import set_priority

    _seed(runtime)
    with pytest.raises(ValueError):
        set_priority(runtime.engine, "garmin", "event:tune", "D")
    with pytest.raises(LookupError):
        set_priority(runtime.engine, "garmin", "event:missing", "A")
    cleared = set_priority(runtime.engine, "garmin", "event:main", None)
    assert cleared["priority"] == "A"


def test_portal_lists_races_and_saves_priority(runtime) -> None:
    today = _seed(runtime)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    listed = client.get("/api/calendar")
    assert listed.status_code == 200
    races = {row["source_id"]: row for row in listed.json()["data"]["races"]}
    assert races["event:tune"]["priority"] is None
    assert races["event:main"]["priority"] == "A"
    saved = client.post(
        "/api/race-priority",
        json={"source": "garmin", "source_id": "event:tune", "priority": "A"},
        headers={"x-vibesynergy": "portal"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["priority"] == "A"
    again = {row["source_id"]: row["priority"] for row in calendar_races(runtime.engine, today)}
    assert again["event:tune"] == "A"
    refused = client.post(
        "/api/race-priority",
        json={"source": "garmin", "source_id": "event:tune", "priority": "D"},
        headers={"x-vibesynergy": "portal"},
    )
    assert refused.status_code == 422


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
    _seed(runtime)
    saved = _call(
        runtime,
        "vibesynergy_race_set_priority",
        source="garmin",
        source_id="event:tune",
        priority="C",
    )
    assert saved["data"]["priority"] == "C"
    state = _call(runtime, "vibesynergy_coach_state")
    races = {row["source_id"]: row["priority"] for row in state["data"]["races"]}
    assert races["event:tune"] == "C"
    assert races["event:main"] == "A"
    with pytest.raises(ValueError, match="race not found"):
        _call(
            runtime,
            "vibesynergy_race_set_priority",
            source="garmin",
            source_id="event:missing",
            priority="A",
        )


def test_effective_priority_uses_the_stored_letter_or_garmin_primary() -> None:
    from vibesynergy.query.race_priority import effective_priority

    assert effective_priority(True, None) == ("A", "derived")
    assert effective_priority(False, None) == (None, "derived")
    assert effective_priority(True, "C") == ("C", "saved")
    assert effective_priority(False, "B") == ("B", "saved")
    assert effective_priority(True, "nope") == ("A", "derived")


def test_saved_a_race_leads_and_a_demoted_primary_keeps_its_flag() -> None:
    from vibesynergy.query.plan import race_phase_from_events

    today = date(2026, 10, 1)
    events = [
        {
            "date": date(2026, 10, 20),
            "title": "Tune-up",
            "is_race": True,
            "primary": False,
            "source": "garmin",
            "source_id": "event:tune",
            "extra": {},
        },
        {
            "date": date(2026, 11, 20),
            "title": "Main",
            "is_race": True,
            "primary": True,
            "source": "caldav",
            "source_id": "uid-main",
            "extra": {},
        },
    ]
    derived = race_phase_from_events(events, today)
    assert derived["next_race"]["source_id"] == "uid-main"
    assert derived["next_race"]["source"] == "caldav"
    assert derived["next_race"]["priority"] == "A"
    assert derived["next_race"]["priority_origin"] == "derived"
    assert derived["next_race"]["is_primary"] is True
    assert derived["supporting"][0]["source_id"] == "event:tune"
    assert derived["supporting"][0]["priority"] is None
    assert derived["supporting"][0]["priority_origin"] == "derived"
    assert derived["supporting"][0]["is_primary"] is False

    saved = race_phase_from_events(
        events,
        today,
        {("garmin", "event:tune"): "A", ("caldav", "uid-main"): "B"},
    )
    assert saved["next_race"]["name"] == "Tune-up"
    assert saved["next_race"]["source_id"] == "event:tune"
    assert saved["next_race"]["priority"] == "A"
    assert saved["next_race"]["priority_origin"] == "saved"
    assert saved["next_race"]["is_primary"] is False
    assert saved["supporting"] == []

    demoted = race_phase_from_events(events, today, {("caldav", "uid-main"): "C"})
    assert demoted["next_race"]["source_id"] == "uid-main"
    assert demoted["next_race"]["priority"] == "C"
    assert demoted["next_race"]["priority_origin"] == "saved"
    assert demoted["next_race"]["is_primary"] is True


def test_plan_and_api_include_source_id_and_priority_origin(runtime) -> None:
    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import dashboard, plan
    from vibesynergy.query.race_priority import set_priority

    today = today_in(runtime.settings.timezone)
    _write(
        runtime,
        [
            PlannedItem("event:club", today + timedelta(days=3), "event", title="Club night"),
            PlannedItem(
                "event:tune", today + timedelta(days=20), "event", title="Tune-up", is_race=True
            ),
            PlannedItem(
                "event:main",
                today + timedelta(days=50),
                "event",
                title="Main race",
                is_race=True,
                primary=True,
            ),
        ],
        key="plan-races",
    )
    _write(
        runtime,
        [
            PlannedItem(
                "event:cal",
                today + timedelta(days=12),
                "event",
                title="Calendar race",
                is_race=True,
            )
        ],
        key="plan-cal",
        source="caldav",
    )
    set_priority(runtime.engine, "caldav", "event:cal", "C")

    out = plan.plan(runtime.engine, Window(today, today), today)
    assert out["next_event"]["source"] == "garmin"
    assert out["next_event"]["source_id"] == "event:club"
    assert out["next_event"]["priority"] is None
    assert out["next_event"]["priority_origin"] == "derived"
    by_id = {row["source_id"]: row for row in out["races"]}
    assert by_id["event:cal"]["source"] == "caldav"
    assert by_id["event:cal"]["priority"] == "C"
    assert by_id["event:cal"]["priority_origin"] == "saved"
    assert by_id["event:tune"]["priority"] is None
    assert by_id["event:tune"]["priority_origin"] == "derived"
    assert by_id["event:main"]["priority"] == "A"
    assert by_id["event:main"]["priority_origin"] == "derived"
    assert by_id["event:main"]["primary"] is True
    assert out["next_race"]["source_id"] == "event:main"
    assert out["next_race"]["priority"] == "A"
    assert out["next_race"]["priority_origin"] == "derived"
    assert out["next_race"]["is_primary"] is True
    assert [row["source_id"] for row in out["supporting"]] == ["event:cal", "event:tune"]
    assert out["supporting"][0]["priority"] == "C"
    assert out["supporting"][0]["priority_origin"] == "saved"
    assert out["supporting"][1]["priority_origin"] == "derived"

    set_priority(runtime.engine, "garmin", "event:tune", "A")
    set_priority(runtime.engine, "garmin", "event:main", "B")
    shifted = plan.plan(runtime.engine, Window(today, today), today)
    assert shifted["next_race"]["source_id"] == "event:tune"
    assert shifted["next_race"]["priority"] == "A"
    assert shifted["next_race"]["priority_origin"] == "saved"
    assert shifted["next_race"]["is_primary"] is False
    assert shifted["plan_phase"] == {"phase": "build", "days_left": 6}
    assert [row["source_id"] for row in shifted["supporting"]] == ["event:cal"]
    assert {row["source_id"]: row["priority"] for row in shifted["races"]}["event:main"] == "B"

    mcp = _call(runtime, "vibesynergy_plan", period="this_week")["data"]
    assert mcp["next_race"]["source_id"] == "event:tune"
    assert mcp["next_race"]["priority"] == "A"
    assert mcp["next_race"]["priority_origin"] == "saved"
    assert mcp["next_race"]["is_primary"] is False
    assert mcp["next_event"]["source_id"] == "event:club"
    assert mcp["supporting"][0]["source_id"] == "event:cal"
    assert mcp["supporting"][0]["priority"] == "C"
    origins = {row["source_id"]: row["priority_origin"] for row in mcp["races"]}
    assert origins["event:main"] == "saved"

    board = dashboard.dashboard(
        runtime.engine,
        today,
        timezone=runtime.settings.timezone,
        athlete=None,
        sources=[],
    )
    assert board["next_race"]["source_id"] == "event:tune"
    assert board["next_race"]["priority"] == "A"
    assert board["next_race"]["priority_origin"] == "saved"
    assert board["plan"]["next_event"]["source_id"] == "event:club"
    assert board["plan"]["races"][0]["source_id"] == "event:cal"
    assert board["supporting"][0]["priority_origin"] == "saved"

    set_priority(runtime.engine, "garmin", "event:tune", None)
    set_priority(runtime.engine, "garmin", "event:main", None)
    restored = plan.race_phase(runtime.engine, today)
    assert restored["next_race"]["source_id"] == "event:main"
    assert restored["next_race"]["priority"] == "A"
    assert restored["next_race"]["priority_origin"] == "derived"
    assert restored["next_race"]["is_primary"] is True
