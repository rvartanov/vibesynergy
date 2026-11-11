"""Public block previews are Garmin statistics only and create no side effects."""

import json
from dataclasses import replace

import anyio
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import func, select
from test_evidence import put
from test_publication import garmin_activity

from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.portal.share import Share
from vibesynergy.query import annotations, publication_format
from vibesynergy.store.schema import job


def test_preview_uses_garmin_statistics_and_leaves_the_registry_unchanged(runtime):
    put(runtime, "garmin", garmin_activity())
    first = annotations.preview(runtime, "garmin:1", language="nl")
    assert first["state"] == "preview" and "🏷️ Effect-label: RECOVERY" in first["block"]
    assert first["publication_authorized"] is False and first["publication_available"] is False
    assert first["assessment_id"] is None
    assert first["plugins"] == {"quote": "off", "weather": "off"}
    assert annotations.preview(runtime, "garmin:1", language="nl") == first
    with runtime.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(job)) == 0
    put(runtime, "garmin", garmin_activity(distance_m=1700))
    again = annotations.preview(runtime, "garmin:1", language="nl")
    assert again["state"] == "preview"
    assert again["block"] == first["block"]


def test_language_and_private_prose_do_not_modify_statistics(runtime):
    a = garmin_activity()
    put(runtime, "garmin", a)
    before = annotations.preview(runtime, "garmin:1")
    put(
        runtime,
        "garmin",
        replace(a, extra={**a.extra, "description": "Human description; never replace it"}),
    )
    assert annotations.preview(runtime, "garmin:1") == before
    dutch = annotations.preview(runtime, "garmin:1", language="nl")
    assert dutch["statistics"] == before["statistics"]
    assert dutch["preview_version"] != before["preview_version"]
    assert dutch["assessment_id"] is None
    assert before["block"].startswith("-Garmin stats:\n  🔥 Aerobic training effect: 1.9\n")
    assert dutch["block"].startswith("-Garmin stats:\n  🔥 Aerobe trainingseffect: 1.9\n")
    assert "Human description" not in dutch["block"]


def test_portal_mcp_share_one_preview_and_share_links_cannot_read_it(runtime):
    put(runtime, "garmin", garmin_activity())
    expected = annotations.preview(runtime, "garmin:1")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    assert (
        client.get("/api/activity-annotation-preview", params={"id": "garmin:1"}).json()["data"]
        == expected
    )
    assert not Share("test", "test", ("activities", "dashboard"), True).allows(
        "GET", "/activity-annotation-preview"
    )

    async def read():
        async with Client(build_server(runtime)) as m:
            return await m.call_tool("vibesynergy_activity_annotation_preview", {"id": "garmin:1"})

    reply = anyio.run(read)
    assert not reply.is_error and reply.structured_content["data"] == expected


def test_preview_rebuilds_numeric_whitelist_instead_of_trusting_prose(runtime, monkeypatch):
    monkeypatch.setattr(
        publication_format,
        "garmin_fields",
        lambda *_: {
            "aerobic_te": 2.0,
            "anaerobic_te": True,
            "te_label": "PRIVATE leaked label",
            "training_load": float("nan"),
            "feel": "PRIVATE",
            "rpe": -4,
            "weather": {"weatherTypeDTO": {"desc": "PRIVATE"}},
        },
    )
    result = annotations.preview(runtime, "synthetic")
    assert result["block"] == "-Garmin stats:\n  🔥 Aerobic training effect: 2.0"
    assert "PRIVATE" not in result["block"]
    assert "PRIVATE" not in json.dumps(result["statistics"])


def test_preview_without_garmin_numbers_is_blocked(runtime, monkeypatch):
    monkeypatch.setattr(publication_format, "garmin_fields", lambda *_: {})
    result = annotations.preview(runtime, "synthetic")
    assert result["state"] == "blocked" and result["reason"] == "no_public_statistics"
