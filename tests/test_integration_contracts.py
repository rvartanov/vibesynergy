"""Shared freshness, ownership and coaching state contracts with synthetic records."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from test_evidence import START, activity, put

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import build_server
from vibesynergy.model import DailyMetric
from vibesynergy.portal.api import NoteRequest, build_router
from vibesynergy.query import assessments, coach, ownership, views
from vibesynergy.store.schema import dataset_state, job

WINDOW = Window(date(2026, 9, 19), date(2026, 9, 22))
AT = datetime(2026, 9, 21, tzinfo=UTC)


def test_metric_freshness_states_and_provenance(runtime):
    empty = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert empty["state"] == "unavailable" and empty["problem"]["stage"] == "fetch"
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.insert().values(
                source="garmin", dataset="test", last_attempt_at=AT, last_success_at=AT
            )
        )
    empty = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert empty["state"] == "empty" and empty["coverage"]["observed_days"] == 0
    raw = runtime.raw.put(
        source="garmin",
        dataset="test",
        item_key="evidence",
        payload=b"pending",
        content_type="text/plain",
    )
    assert (
        views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)["state"]
        == "pending"
    )
    runtime.raw.mark_normalised(raw.id, error="synthetic mapping error")
    failed = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert failed["state"] == "unavailable" and failed["problem"]["stage"] == "mapping"
    put(runtime, "garmin", DailyMetric(date(2026, 9, 20), "sleep.duration", 25000, "s"))
    for item in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(item)
    stale = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert stale["state"] == "stale"
    assert stale["unit"] == "s" and stale["coverage"]["observed_days"] == 1
    assert stale["observations"][0]["source"] == "garmin"
    assert stale["observations"][0]["fetched_at"] is not None
    assert stale["observations"][0]["mapped_at"] is not None
    put(runtime, "garmin", DailyMetric(date(2026, 9, 22), "sleep.duration", 26000, "s"))
    for item in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(item)
    current = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert current["state"] == "current"
    assert (
        current["version"]
        != views.metric_status(
            runtime.engine,
            "sleep.duration",
            "garmin",
            "test",
            Window(date(2026, 9, 20), date(2026, 9, 22)),
        )["version"]
    )
    same = views.metric_status(
        runtime.engine,
        "sleep.duration",
        "garmin",
        "test",
        WINDOW,
        previous_version=current["version"],
    )
    assert same["state"] == "unchanged" and same["freshness"] == "current"
    assert same["version"] == current["version"]
    put(runtime, "garmin", DailyMetric(date(2026, 9, 22), "sleep.duration", 26000, "s"))
    assert (
        views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)["version"]
        == current["version"]
    )
    put(runtime, "garmin", DailyMetric(date(2026, 9, 22), "sleep.duration", 27000, "s"))
    assert (
        views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)["version"]
        != current["version"]
    )


def test_fetch_error_is_dataset_scoped(runtime):
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.insert().values(
                source="garmin",
                dataset="test",
                last_attempt_at=AT,
                last_success_at=AT,
                last_error="synthetic rate limit",
            )
        )
    status = views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    assert status["state"] == "unavailable"
    assert status["problem"]["source"] == "garmin"
    assert status["problem"]["dataset"] == "test"
    assert status["problem"]["stage"] == "fetch"


def test_processed_import_without_metric_is_empty_not_missing_fetch(runtime):
    raw = runtime.raw.put(
        source="garmin_export",
        dataset="files",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    runtime.raw.mark_normalised(raw.id)
    status = views.metric_status(runtime.engine, "sleep.duration", "garmin_export", "files", WINDOW)
    assert status["collection"]["fetch_status"] == "not_recorded"
    assert status["state"] == "empty"


def test_ownership_requires_durable_origin_evidence():
    target = ownership.ExternalObjectRef(
        provider="garmin",
        account="synthetic",
        calendar="training",
        external_id="object-1",
        etag="v1",
    )
    claim = ownership.OwnershipClaim(
        target=target,
        origin="vibesynergy",
        origin_id="proposal-1",
        ledger_id="ledger-1",
        verified=True,
    )
    assert ownership.can_write(claim)
    assert ownership.idempotency_key(claim, "update", "proposal-1") == ownership.idempotency_key(
        claim, "update", "proposal-1"
    )
    assert not ownership.can_write(claim.model_copy(update={"verified": False}))
    assert not ownership.can_write(claim.model_copy(update={"origin": "garmin"}))
    with pytest.raises(ValueError):
        ownership.OwnershipClaim(target=target, origin="vibesynergy", verified=True)
    create = ownership.CreateIntent(
        provider="garmin",
        account="synthetic",
        calendar="training",
        origin_id="proposal-1",
        ledger_id="ledger-1",
        intent_id="intent-1",
    )
    assert ownership.create_idempotency_key(create) == ownership.create_idempotency_key(create)
    assert ownership.create_idempotency_key(create) != ownership.create_idempotency_key(
        create.model_copy(update={"intent_id": "intent-2"})
    )


def test_shared_coaching_state_is_retired_and_read_does_not_write(runtime):
    put(runtime, "garmin", activity())
    ready = assessments.read(runtime, "garmin:1")
    assert ready["state"] == "missing"
    assert ready["coaching_state"]["reason"] == "retired"
    assert ready["coaching_state"]["freshness"] == "unavailable"
    assert ready["coaching_state"]["activity_id"] == "garmin:1"
    put(runtime, "garmin", replace(activity(), distance_m=1700))
    again = assessments.read(runtime, "garmin:1")
    assert again["state"] == "missing"
    assert again["assessment"] is None


def test_selected_dataset_status_matches_full_status(runtime):
    put(runtime, "garmin", activity())
    put(runtime, "strava", replace(activity(), source_id="2", fit_id="strava"))
    all_datasets = views.sync_status(runtime.engine)["datasets"]
    selected = views.sync_status(runtime.engine, datasets={("garmin", "test")})["datasets"]
    assert selected == [
        d for d in all_datasets if (d["source"], d["dataset"]) == ("garmin", "test")
    ]
    assert views.sync_status(runtime.engine, datasets=set())["datasets"] == []


def test_diary_changes_do_not_queue_internal_assessments(runtime):
    put(runtime, "garmin", activity())
    routes = {
        (route.path, method): route.endpoint
        for route in build_router(runtime).routes
        for method in route.methods
    }
    routes["/diary/note", "POST"](NoteRequest(text="Synthetic diary", date=str(START.date())))
    server = build_server(runtime)
    server._tool_manager.get_tool("vibesynergy_add_note").fn(
        "Another synthetic note", date=str(START.date())
    )
    assert assessments.read(runtime, "garmin:1")["state"] == "missing"
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(job)) == 0


def test_mcp_coaching_and_metric_reads_share_the_query_and_write_nothing(runtime):
    coach.save_draft(runtime.engine, "policy", {}, expected_version=0, reason="test")
    coach.activate(runtime.engine, "policy", expected_version=1, reason="test")
    put(runtime, "garmin", activity())
    with runtime.engine.connect() as conn:
        before = conn.scalar(select(func.count()).select_from(job))

    server = build_server(runtime)
    registered = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert len(registered) == 112
    assert registered["vibesynergy_model_run_write"].annotations.read_only_hint is False
    assert registered["vibesynergy_model_runs"].annotations.read_only_hint is True
    assert registered["vibesynergy_race_set_priority"].annotations.read_only_hint is False
    assert registered["vibesynergy_metric_status"].annotations.read_only_hint is True
    assert "vibesynergy_current_coaching_state" not in registered
    metric = server._tool_manager.get_tool("vibesynergy_metric_status").fn(
        "sleep.duration", "garmin", "test", "2026-09-19", "2026-09-22"
    )["data"]
    assert metric == views.metric_status(runtime.engine, "sleep.duration", "garmin", "test", WINDOW)
    activity_read = server._tool_manager.get_tool("vibesynergy_activity_assessment").fn("garmin:1")[
        "data"
    ]
    assert activity_read["state"] == "missing"
    assert activity_read["coaching_state"]["reason"] == "retired"
    with runtime.engine.connect() as conn:
        after = conn.scalar(select(func.count()).select_from(job))
    assert after == before


def test_retired_internal_assessment_routes_are_gone(runtime):
    """The old 410 stubs, the empty recent list and the portal's assessment read are removed;
    the MCP read (vibesynergy_activity_assessment) stays."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    headers = {"x-vibesynergy": "portal"}
    post = client.post("/api/activity-assessment", json={"id": "x"}, headers=headers)
    assert post.status_code == 404
    assert client.get("/api/activity-assessment", params={"id": "x"}).status_code == 404
    post = client.post("/api/daily-coaching", json={"kind": "morning"}, headers=headers)
    assert post.status_code == 405
    assert client.get("/api/activity-assessments/recent").status_code == 404
    assert client.get("/api/daily-coaching").status_code == 200
    assert not hasattr(assessments, "prepare") and not hasattr(assessments, "complete")
    # No portal module still calls them.
    static = Path(__file__).parents[1] / "src/vibesynergy/portal/static/js"
    for module in static.rglob("*.js"):
        text = module.read_text()
        assert "activity-assessments/recent" not in text, module
        assert 'post("activity-assessment"' not in text, module
        assert 'post("daily-coaching"' not in text, module
