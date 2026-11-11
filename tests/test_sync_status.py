"""A successful fetch, archive processing, and observed coverage are distinct claims."""

from datetime import UTC, date, datetime

import anyio
from fastapi.testclient import TestClient
from mcp import Client

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import views
from vibesynergy.store.schema import dataset_state

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def put(rt, source="garmin_export", key="file", payload=b"bad"):
    return rt.raw.put(
        source=source,
        dataset="files",
        item_key=key,
        payload=payload,
        content_type="application/json",
    )


def test_import_errors_are_visible_without_a_fetch_state(runtime):
    raw = put(runtime)
    runtime.raw.mark_normalised(raw.id, error="JSONDecodeError: test failure")
    status = views.sync_status(runtime.engine)
    item = status["problems"][0]
    assert item["source"] == "garmin_export"
    assert item["last_error"] is None  # a processing error is not a failed download
    assert item["fetch_status"] == "not_recorded"
    assert item["problem_stages"] == ["mapping"]
    assert item["processing"]["errors"] == 1
    assert item["processing"]["last_attempt_at"] is not None
    assert "Mapping failed" in item["problem"]
    assert views.sync_status(runtime.engine, "strava")["datasets"] == []


def test_fetch_success_does_not_hide_processing_errors(runtime):
    raw = put(runtime, "garmin")
    runtime.raw.mark_normalised(raw.id, error="ValueError: sample")
    with runtime.engine.begin() as c:
        c.execute(
            dataset_state.insert().values(
                source="garmin",
                dataset="files",
                last_attempt_at=NOW,
                last_success_at=NOW,
            )
        )
    first = views.sync_status(runtime.engine)["problems"][0]
    assert first["fetch_status"] == "succeeded"
    assert first["processing"]["state"] == "failed"
    with runtime.engine.begin() as c:
        c.execute(dataset_state.update().values(last_error="RateLimited: try later"))
    second = views.sync_status(runtime.engine)["problems"][0]
    assert second["last_success_at"] == NOW.isoformat()  # previous success remains visible
    assert second["problem_stages"] == ["fetch", "mapping"]
    assert "RateLimited" in second["problem"] and "Mapping failed" in second["problem"]


def test_replaced_failures_do_not_poison_current_status(runtime):
    old = put(runtime)
    runtime.raw.mark_normalised(old.id, error="ValueError: old version")
    new = put(runtime, payload=b"[]")
    pending = views.sync_status(runtime.engine)
    assert pending["problems"] == []
    assert pending["datasets"][0]["processing"]["state"] == "pending"
    assert pending["datasets"][0]["processing"]["current_items"] == 1
    runtime.raw.mark_normalised(new.id)
    done = views.sync_status(runtime.engine)
    assert done["datasets"][0]["processing"]["state"] == "processed"
    assert runtime.raw.stats()[0]["errors"] == 1  # historical error retained in raw archive
    assert done["problems"] == []


def test_empty_fetch_does_not_claim_processing_or_coverage(runtime):
    with runtime.engine.begin() as c:
        c.execute(
            dataset_state.insert().values(
                source="demo",
                dataset="daily",
                last_attempt_at=NOW,
                last_success_at=NOW,
            )
        )
    status = views.sync_status(runtime.engine)
    assert status["datasets"][0]["processing"]["state"] == "empty"
    assert status["semantics"]["last_success_at"] == "collection_or_import_only"
    assert status["problems"] == []


def test_portal_and_mcp_share_stage_errors(runtime):
    raw = put(runtime)
    runtime.raw.mark_normalised(raw.id, error="ValueError: synthetic mapping error")
    portal = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    data = portal.get("/api/data").json()["data"]
    assert any(s["id"] == "garmin_export" for s in data["sources"])
    assert portal.get("/api/meta").json()["data"]["problems"] == 1
    assert portal.get("/api/settings").status_code == 200

    async def read():
        async with Client(build_server(runtime)) as client:
            reply = await client.call_tool("vibesynergy_sync_status", {})
            return reply.structured_content["data"]

    mcp = anyio.run(read)
    assert mcp["problems"] == data["problems"]
    assert data["status_semantics"] == mcp["semantics"]
    assert views.overview(runtime.engine, NOW.date())["sync_problems"][0]["stages"] == ["mapping"]


def test_coverage_denominator_excludes_days_outside_query(runtime):
    current = views.coverage(
        runtime.engine, ["daily.steps"], Window(date(2026, 9, 28), date(2026, 10, 1))
    )
    assert current["days_in_window"] == {"2026-09": 3, "2026-10": 1}
    leap = views.coverage(
        runtime.engine, ["daily.steps"], Window(date(2024, 2, 28), date(2024, 3, 1))
    )
    assert leap["days_in_window"] == {"2024-02": 2, "2024-03": 1}
