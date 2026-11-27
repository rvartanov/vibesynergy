"""The same race/phase projection reaches daily, MCP, portal and coaching reads."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Request
from tests_helpers import add_race

from vibesynergy.core.timeutil import today_in
from vibesynergy.mcp.server import build_server
from vibesynergy.model import PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.api import build_router
from vibesynergy.query import plan, views


def _race(runtime, day):
    # A Garmin event of the same race is a copy at most: the race is VibeSynergy's (#111).
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic-race",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [PlannedItem("event:synthetic-race", day, "event", title="Ultra", is_race=True)],
            source="garmin",
            raw_item_id=raw.id,
        )
    return add_race(
        runtime.engine,
        "Synthetic Ultra",
        day,
        "A",
        distance_m=75000.0,
        links={"garmin": "event:synthetic-race"},
    )


def test_no_race_is_null_in_shared_and_mcp_outputs(runtime):
    today = today_in(runtime.settings.timezone)
    expected = {"next_race": None, "plan_phase": None}
    assert plan.race_phase(runtime.engine, today) == expected
    server = build_server(runtime)
    for tool, args in (("vibesynergy_day", {"date": str(today)}), ("vibesynergy_overview", {})):
        data = server._tool_manager.get_tool(tool).fn(**args)["data"]
        assert {key: data[key] for key in expected} == expected


def test_taper_starts_fourteen_days_before_race():
    from datetime import date

    race = {
        "date": date(2026, 10, 10),
        "title": "Synthetic Ultra",
        "is_race": True,
        "primary": False,
        "extra": {},
    }
    assert plan.race_phase_from_events([race], date(2026, 9, 26))["plan_phase"] == {
        "phase": "taper",
        "days_left": 14,
    }
    assert plan.race_phase_from_events([race], date(2026, 9, 25))["plan_phase"] == {
        "phase": "build",
        "days_left": 1,
    }


def test_race_in_nine_days_reaches_mcp_portal_and_coaching_state(runtime):
    today = today_in(runtime.settings.timezone)
    race_id = _race(runtime, today + timedelta(days=9))
    expected_race = {
        "name": "Synthetic Ultra",
        "date": str(today + timedelta(days=9)),
        "days_to_go": 9,
        "distance_m": 75000,
        "source": "vibesynergy",
        "source_id": race_id,
        "priority": "A",
        "priority_origin": "saved",
        "is_primary": True,
    }
    expected_phase = {"phase": "taper", "days_left": 9}
    server = build_server(runtime)
    for tool, args in (("vibesynergy_day", {"date": str(today)}), ("vibesynergy_overview", {})):
        data = server._tool_manager.get_tool(tool).fn(**args)["data"]
        assert data["next_race"] == expected_race
        assert data["plan_phase"] == expected_phase
    routes = {
        (route.path, method): route.endpoint
        for route in build_router(runtime).routes
        for method in route.methods
    }
    request = Request({"type": "http", "method": "GET", "path": "/today", "headers": []})
    daily = routes["/today", "GET"](request, date=str(today))["data"]["day"]
    dashboard_request = Request(
        {"type": "http", "method": "GET", "path": "/dashboard", "headers": []}
    )
    dashboard = routes["/dashboard", "GET"](dashboard_request)["data"]
    for data in (daily, dashboard, views.day(runtime.engine, today)):
        assert data["next_race"] == expected_race
        assert data["plan_phase"] == expected_phase
