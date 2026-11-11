"""The same race/phase projection reaches daily, MCP, portal and coaching reads."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

from fastapi import Request

from vibesynergy.core.timeutil import today_in
from vibesynergy.mcp.server import build_server
from vibesynergy.model import PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.api import build_router
from vibesynergy.query import assessments, plan, views


def _race(runtime, day):
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
            [
                PlannedItem(
                    "event:synthetic-race",
                    day,
                    "event",
                    title="Synthetic Ultra",
                    is_race=True,
                    primary=True,
                    extra={"completionTarget": {"unitType": "distance", "value": 75}},
                )
            ],
            source="garmin",
            raw_item_id=raw.id,
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
    _race(runtime, today + timedelta(days=9))
    expected_race = {
        "name": "Synthetic Ultra",
        "date": str(today + timedelta(days=9)),
        "days_to_go": 9,
        "distance_m": 75000,
        "source": "garmin",
        "source_id": "event:synthetic-race",
        "priority": "A",
        "priority_origin": "derived",
        "is_primary": True,
    }
    expected_phase = {"phase": "taper", "days_left": 9}
    server = build_server(runtime)
    for tool, args in (("vibesynergy_day", {"date": str(today)}), ("vibesynergy_overview", {})):
        data = server._tool_manager.get_tool(tool).fn(**args)["data"]
        assert data["next_race"] == expected_race
        assert data["plan_phase"] == expected_phase
    state = assessments.current_state(runtime)
    assert state["next_race"] == expected_race
    assert state["plan_phase"] == expected_phase
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


def test_race_phase_renders_in_english_and_dutch():
    static = Path(__file__).parents[1] / "src/vibesynergy/portal/static"
    script = """
import fs from 'node:fs';
import { setLanguage } from './js/i18n.js';
import { racePhaseHtml } from './js/race_phase.js';
globalThis.document = { documentElement: { lang: 'en' } };
globalThis.fetch = async (url) => ({ json: async () => {
  const file = String(url).includes('nl.json') ? './i18n/nl.json' : './i18n/en.json';
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}});
const data = {next_race: {name: 'Synthetic Ultra', date: '2026-10-10', days_to_go: 9,
  distance_m: 75000, priority: 'A'}, plan_phase: {phase: 'taper', days_left: 9}};
const singular = {next_race: {name: 'Synthetic Ultra', date: '2026-11-28', days_to_go: 40,
  distance_m: 75000, priority: 'B'}, plan_phase: {phase: 'build', days_left: 1},
  supporting: [{name: 'Sallandse 30 km', date: '2026-10-31', days_to_go: 15}]};
for (const language of ['en', 'nl']) {
  await setLanguage(language);
  process.stdout.write(JSON.stringify({language, html: racePhaseHtml(data)}) + '\\n');
  const one = JSON.stringify({language: language + '-one', html: racePhaseHtml(singular)});
  process.stdout.write(one + '\\n');
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=static,
        capture_output=True,
        text=True,
        check=True,
    )
    rows = {row["language"]: row["html"] for row in map(json.loads, result.stdout.splitlines())}
    en, nl = rows["en"], rows["nl"]
    assert "Next race and plan phase" in en and "Taper" in en and "9 days to go" in en
    assert "Volgende wedstrijd en planfase" in nl and "Afbouw" in nl and "Nog 9 dagen" in nl
    assert "Synthetic Ultra" in en and "Synthetic Ultra" in nl
    assert "Primary race" in en and "Other race" not in en
    assert "Hoofdwedstrijd" in nl and "Andere wedstrijd" not in nl
    assert "Supporting" in rows["en-one"] and "Other race" not in rows["en-one"]
    assert "Ondersteunend" in rows["nl-one"]
    assert "1 day left in phase" in rows["en-one"]
    assert "days left in phase" not in rows["en-one"]
    assert "Tune-up: Sallandse 30 km in 15 days" in rows["en-one"]
    assert "Nog 1 dag in deze fase" in rows["nl-one"]
    assert "Tussenwedstrijd: Sallandse 30 km over 15 dagen" in rows["nl-one"]
