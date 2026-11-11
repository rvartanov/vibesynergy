"""Settings task G: live sync intervals, local thresholds, coach notes."""

from __future__ import annotations

import json

import anyio
from fastapi.testclient import TestClient
from mcp import Client

from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import athlete_threshold, coach
from vibesynergy.sync.scheduler import Scheduler

WRITE = {"x-vibesynergy": "portal", "origin": "http://127.0.0.1:8447"}


def _client(runtime) -> TestClient:
    return TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")


def test_schedule_save_reloads_without_queueing_or_restart(runtime) -> None:
    now = [0.0]
    scheduler = Scheduler(runtime.settings, runtime.registry, runtime.jobs, clock=lambda: now[0])
    runtime.scheduler = scheduler
    assert "dropfolder" in scheduler.run_once()
    assert scheduler._next["dropfolder"] == 300
    before = [job["id"] for job in runtime.jobs.recent(limit=20)]
    reply = _client(runtime).post(
        "/api/settings",
        json={"changes": {"schedule": {"dropfolder": 1}}},
        headers=WRITE,
    )
    assert reply.status_code == 200
    body = reply.json()["data"]
    assert body["schedule"]["configured"]["dropfolder"] == 1
    assert body["restart_needed"] is False
    assert scheduler._next["dropfolder"] == 60
    assert [job["id"] for job in runtime.jobs.recent(limit=20)] == before


def test_threshold_edit_stays_local(runtime) -> None:
    reply = _client(runtime).post(
        "/api/athlete/thresholds",
        json={"lthr": 170, "threshold_pace": 4.5},
        headers=WRITE,
    )
    assert reply.status_code == 200
    saved = reply.json()["data"]["thresholds"]
    assert saved["lthr"]["value"] == 170
    assert saved["lthr"]["unit"] == "bpm"
    assert saved["threshold_pace"]["value"] == 4.5
    assert saved["threshold_pace"]["unit"] == "min/km"
    current = athlete_threshold.current_thresholds(runtime.engine)["run"]
    assert current["lthr"]["value"] == 170
    too_high = _client(runtime).post("/api/athlete/thresholds", json={"lthr": 20}, headers=WRITE)
    assert too_high.status_code == 400


def test_coach_notes_are_readable_and_not_writable_by_the_tool(runtime) -> None:
    client = _client(runtime)
    state = client.get("/api/coach").json()["data"]
    payload = state["policy"]["active"]["payload"]
    payload["instructions"] = "Keep the long run easy."
    saved = client.post(
        "/api/coach/policy/apply",
        json={"payload": payload, "expected_version": state["policy"]["version"]},
        headers=WRITE,
    )
    assert saved.status_code == 200
    assert coach.athlete_notes(runtime.engine) == "Keep the long run easy."

    async def go():
        async with Client(build_server(runtime)) as mcp:
            tool = await mcp.call_tool("vibesynergy_athlete", {})
            resource = await mcp.read_resource("vibesynergy://athlete/notes")
            listed = await mcp.list_tools()
            return tool, resource, listed

    tool, resource, listed = anyio.run(go)
    text = tool.content[0].text if tool.content else ""
    data = (tool.structured_content or json.loads(text))["data"]
    assert data["notes"] == "Keep the long run easy."
    blob = resource.contents[0].text
    assert json.loads(blob) == {"notes": "Keep the long run easy."}
    athlete = next(item for item in listed.tools if item.name == "vibesynergy_athlete")
    assert athlete.annotations.read_only_hint is True
