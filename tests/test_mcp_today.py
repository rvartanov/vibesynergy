"""Coach-shaped reads: one today call, a full week, and a typed coach_write schema."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import anyio
from mcp import Client

from vibesynergy.derive import unify
from vibesynergy.mcp.server import build_server
from vibesynergy.model import Activity, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import dashboard, journal

FRIDAY = date(2026, 10, 16)
ROOT = Path(__file__).parents[1]


def call(rt, tool: str, **args) -> dict:
    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise ValueError(text)
    return result.structured_content or json.loads(text)


def _seed(runtime) -> None:
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="coach-reads",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "tempo",
                    FRIDAY,
                    "plan_workout",
                    "Tempoloop 13.5 km",
                    sport="running",
                ),
                PlannedItem(
                    "sat",
                    date(2026, 10, 17),
                    "plan_workout",
                    "Long run",
                    sport="running",
                ),
                PlannedItem(
                    "sun",
                    date(2026, 10, 18),
                    "plan_workout",
                    "Easy",
                    sport="running",
                ),
                PlannedItem(
                    "event:veluwe",
                    date(2026, 11, 28),
                    "event",
                    title="Veluwe Ultra Trail 75 km",
                    is_race=True,
                    primary=True,
                ),
                Activity(
                    "long",
                    datetime(2026, 10, 16, 7, tzinfo=UTC),
                    FRIDAY,
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
    journal.set_checkin(runtime.engine, FRIDAY, mood=3, note="Legs heavy")


def test_today_answers_in_one_call(runtime, monkeypatch) -> None:
    assert FRIDAY.weekday() == 4
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: FRIDAY)
    _seed(runtime)
    out = call(runtime, "vibesynergy_today")
    data = out["data"]
    assert data["safety"]["level"]
    assert data["checkin"]["mood"] == 3
    assert data["planned_today"]["title"] == "Tempoloop 13.5 km"
    assert data["planned_today"]["status"] == "changed"
    assert data["race"]["name"] == "Veluwe Ultra Trail 75 km"
    assert len(json.dumps(out)) < 16000
    board = dashboard.dashboard(
        runtime.engine,
        FRIDAY,
        timezone=runtime.settings.timezone,
        athlete=runtime.settings.athlete,
        sources=[],
    )
    assert board["plan"]["today"][0]["status"] == "changed"
    assert "differed from the plan" in board["verdict"]["advice"]


def test_friday_week_includes_the_weekend_and_races(runtime, monkeypatch) -> None:
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: FRIDAY)
    _seed(runtime)
    plan = call(runtime, "vibesynergy_plan", period="this_week")
    assert plan["meta"]["period"] == {"start": "2026-10-12", "end": "2026-10-18"}
    titles = {item["date"]: item["title"] for item in plan["data"]["items"]}
    assert titles["2026-10-17"] == "Long run"
    assert titles["2026-10-18"] == "Easy"
    week = call(runtime, "vibesynergy_week")
    assert week["meta"]["period"]["end"] == "2026-10-18"
    changed = [item for item in week["data"]["items"] if item["status"] == "changed"]
    assert changed[0]["title"] == "Tempoloop 13.5 km"
    assert changed[0]["matched"]["distance_m"] == 31000
    assert week["data"]["totals"]["changed"] == 1
    state = call(runtime, "vibesynergy_coach_state")
    assert state["data"]["races"]
    assert state["data"]["races"][0]["name"] == "Veluwe Ultra Trail 75 km"
    assert not state["data"].get("coaching_events")


def test_coach_write_entry_type_is_an_enum(runtime) -> None:
    async def go():
        async with Client(build_server(runtime)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["vibesynergy_coach_write"]

    tool = anyio.run(go)
    raw = tool.input_schema
    schema = raw.model_dump() if hasattr(raw, "model_dump") else raw
    entry = schema["properties"]["entry_type"]
    enum = entry.get("enum")
    if not enum:
        enum = [item.get("const") for item in entry.get("anyOf", []) if item.get("const")]
    assert enum == [
        "activity_assessment",
        "morning",
        "evening",
        "note",
        "threshold_change",
    ]
    chip = (ROOT / "src/vibesynergy/portal/static/js/pages/dashboard.js").read_text()
    assert 'changed: ["warning", N("≈ changed")]' in chip
