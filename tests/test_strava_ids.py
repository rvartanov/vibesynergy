"""Strava ids past 2^53 stay exact strings end to end (#87).

From 1 June 2027 Strava ids may need all 64 bits. Python reads them exactly, but a browser's
JSON.parse rounds a number above 2^53, so a link would point at the wrong activity. These
ids go through fetch, map, store, the webhook, the portal API, MCP and the portal's own
link code, and must come out unchanged, as strings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select
from strava_fake import RIDE, FakeStravaAPI

from vibesynergy.api.app import WEBHOOK, create_app
from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.strava.client import StravaClient
from vibesynergy.sources.strava.ids import same_id, strava_id
from vibesynergy.store.schema import activity, activity_lap, audit_log

DAY = date(2026, 9, 20)
ACTIVITY = "9007199254740993"  # 2^53 + 1: a float rounds it to ...992
ATHLETE = "9007199254740995"  # a float rounds it to ...996
UPLOAD = "9007199254740997"
HOOK = "/vibesynergy/strava/webhook/stok"
ROOT = Path(__file__).parents[1]


class BigIdStrava(FakeStravaAPI):
    """The fake Strava with one activity and an athlete whose ids are past 2^53, in the
    shape Strava announced: ``id`` a number, ``id_str`` the same value as a string."""

    def _body(self, path: str, params: dict) -> Any:
        summary = {
            **RIDE,
            "id": int(ACTIVITY),
            "id_str": ACTIVITY,
            "upload_id": int(UPLOAD),
            "upload_id_str": UPLOAD,
        }
        if path == "/athlete":
            return {"id": int(ATHLETE), "id_str": ATHLETE, "bikes": [], "shoes": []}
        if path == "/athlete/activities":
            return [summary] if params.get("page") == 1 else []
        if path == f"/activities/{ACTIVITY}":
            return {**summary, "athlete": {"id": int(ATHLETE), "id_str": ATHLETE}}
        if path.startswith(("/activities/", "/athletes/")) and not path.endswith(
            ("/laps", "/zones", "/streams", "/stats", "/routes")
        ):
            return {"message": f"unexpected {path}"}
        return super()._body(path, params)


@pytest.fixture
def big(runtime):
    settings = replace(
        runtime.settings,
        sources={"strava": {"enabled": True}},
        server={"public_url": "https://server.example.ts.net"},
    )
    rt = Runtime.open(settings)
    source = rt.registry.get("strava")
    fake = BigIdStrava()
    store = source.ctx.secrets
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "ssecret"})
    oauth.save_tokens(store, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    # Stored as a number, as before #87: it is still compared correctly, as a string.
    store.set("athlete", {"id": int(ATHLETE)})
    store.set(WEBHOOK, {"verify_token": "vt", "token": "stok", "subscription_id": 5})
    source._client = StravaClient(store, http=fake, sleep=lambda s: None, clock=lambda: 1000.0)
    yield rt, fake
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def _no_unsafe_numbers(value: Any, where: str = "$") -> None:
    """No number in this JSON that JSON.parse would round (|n| > 2^53)."""
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        assert abs(value) <= 2**53, f"{where} = {value} is rounded by a browser"
    elif isinstance(value, dict):
        for k, v in value.items():
            _no_unsafe_numbers(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_unsafe_numbers(v, f"{where}[{i}]")


def _drain(rt) -> None:
    worker = rt.worker()
    while worker.run_once():
        pass


def test_id_str_wins_and_ids_compare_as_strings() -> None:
    assert strava_id({"id": 1, "id_str": "9007199254740993"}) == "9007199254740993"
    assert strava_id({"id": 9007199254740993}) == "9007199254740993"
    assert strava_id({"object_id": 5, "object_id_str": "6"}, "object_id") == "6"
    assert strava_id({"id": 5, "id_str": "not a number"}) == "5"
    # A float has lost its digits already; booleans, negatives and paths are no ids.
    for bad in (9007199254740992.0, True, -1, 0, "9001/../athlete", "", None, 2**64):
        assert strava_id({"id": bad}) is None
    assert strava_id(None) is None
    assert same_id(9007199254740995, "9007199254740995")
    assert not same_id("9007199254740995", "9007199254740996")  # equal as floats
    assert not same_id(None, "1") and not same_id("1", None)


def test_big_ids_from_fetch_to_portal_and_mcp(big) -> None:
    rt, fake = big
    results = rt.sync.sync(
        "strava", ["profile", "activities", "activity_streams"], Window(DAY, DAY)
    )
    assert {r.dataset: r.error for r in results if r.error or r.normalise_errors} == {}
    assert f"/athletes/{ATHLETE}/stats" in fake.calls
    assert f"/activities/{ACTIVITY}/laps" in fake.calls

    # Stored: the canonical row, its laps and stream, all under the exact string.
    (row,) = _rows(rt, activity, activity.c.source == "strava")
    assert row["source_id"] == ACTIVITY
    assert row["fit_id"] == f"strava-{ACTIVITY}"
    assert row["extra"]["upload_id"] == UPLOAD
    assert {lap["activity_source_id"] for lap in _rows(rt, activity_lap)} == {ACTIVITY}
    assert rt.parquet.stream_path(f"strava-{ACTIVITY}").exists()

    # Webhook: the numbers Strava sends become the same strings.
    client = TestClient(create_app(rt))
    event = {
        "object_type": "activity",
        "object_id": int(ACTIVITY),
        "aspect_type": "update",
        "owner_id": int(ATHLETE),
        "subscription_id": 5,
    }
    # Another athlete whose id is the same as a float: not ours.
    other = {**event, "owner_id": int(ATHLETE) + 1}
    assert client.post(HOOK, json=other).status_code == 403
    assert client.post(HOOK, json=event).status_code == 200
    (job,) = rt.jobs.recent()
    assert job["payload"]["key"] == ACTIVITY
    evidence = _rows(rt, audit_log, audit_log.c.action == "strava.event")[-1]["detail"]
    assert evidence["object_id"] == ACTIVITY
    _drain(rt)
    assert fake.calls.count(f"/activities/{ACTIVITY}") >= 1
    # A string form, if Strava adds one, is read first.
    as_strings = {**event, "object_id_str": ACTIVITY, "owner_id_str": ATHLETE}
    assert client.post(HOOK, json={**as_strings, "object_id": 1, "owner_id": 1}).status_code == 200
    assert rt.jobs.recent()[0]["payload"]["key"] == ACTIVITY
    _drain(rt)
    assert [r["source_id"] for r in _rows(rt, activity, activity.c.source == "strava")] == [
        ACTIVITY
    ]

    # Portal API: the id is a string, and no number in the JSON is past 2^53.
    portal = TestClient(create_portal_app(rt), base_url="https://server.example.ts.net:8447")
    listing = portal.get("/api/activities", params={"start": str(DAY), "end": str(DAY)})
    assert listing.status_code == 200
    _no_unsafe_numbers(listing.json())
    (item,) = listing.json()["data"]["activities"]
    assert item["group_id"] == f"strava:{ACTIVITY}"
    detail = portal.get("/api/activity", params={"id": f"strava:{ACTIVITY}"})
    assert detail.status_code == 200
    _no_unsafe_numbers(detail.json())
    data = detail.json()["data"]
    assert data["activity"]["group_id"] == f"strava:{ACTIVITY}"
    assert data["sources"][0]["id"] == f"strava:{ACTIVITY}"
    assert data["sources"][0]["extra"]["upload_id"] == UPLOAD

    # MCP: the same strings.
    async def mcp(tool: str, **args):
        async with Client(build_server(rt)) as mcp_client:
            return await mcp_client.call_tool(tool, args)

    for tool, args in (
        ("vibesynergy_activities", {"start": str(DAY), "end": str(DAY)}),
        ("vibesynergy_activity", {"id": f"strava:{ACTIVITY}"}),
    ):
        result = anyio.run(lambda tool=tool, args=args: mcp(tool, **args))
        assert not result.is_error, result.content
        text = result.content[0].text
        _no_unsafe_numbers(json.loads(text))
        assert f"strava:{ACTIVITY}" in text

    # The portal's own code: the link it builds from that very JSON carries the exact id.
    _portal_link_check(listing.text, detail.text)


def _portal_link_check(listing: str, detail: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    module = (ROOT / "src/vibesynergy/portal/static/js/workout_ref.js").as_uri()
    script = f"""
import {{ workoutRefHtml }} from {json.dumps(module)};
const [listing, detail] = JSON.parse(await new Promise((done) => {{
  let text = "";
  process.stdin.on("data", (c) => (text += c));
  process.stdin.on("end", () => done(text));
}}));
const row = JSON.parse(listing).data.activities[0];
const id = row.sources?.[0] ?? row.group_id;  // what the activities page links to
const page = JSON.parse(detail).data;
console.log(JSON.stringify({{
  activities: `#/activity/${{encodeURIComponent(id)}}`,
  ref: workoutRefHtml({{ title: "Ride", activity_id: page.activity.group_id }}),
}}));
"""
    out = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps([listing, detail]),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    links = json.loads(out.stdout)
    href = f"#/activity/strava%3A{ACTIVITY}"
    assert links["activities"] == href
    assert f'href="{href}"' in links["ref"]
