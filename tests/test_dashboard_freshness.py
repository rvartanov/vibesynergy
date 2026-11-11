"""Dashboard evidence status uses the shared source/dataset metric contract."""

from datetime import timedelta

from starlette.requests import Request
from test_evidence import put

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.model import DailyMetric
from vibesynergy.portal.api import build_router
from vibesynergy.portal.share import Share
from vibesynergy.query import views
from vibesynergy.store.schema import dataset_state


def test_selected_metric_status_tracks_correction_and_failure(runtime):
    today = today_in(runtime.settings.timezone)
    window = Window(today - timedelta(days=13), today)
    assert views.selected_metric_status(runtime.engine, "sleep.duration", window) is None

    put(runtime, "garmin", DailyMetric(today, "sleep.duration", 25000, "s"))
    for item in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(item)
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.insert().values(source="garmin", dataset="test", last_success_at=None)
        )
    first = views.selected_metric_status(runtime.engine, "sleep.duration", window)
    assert first["source"] == "garmin" and first["dataset"] == "test"
    assert first["observations"][-1]["value"] == 25000
    put(runtime, "garmin", DailyMetric(today, "sleep.duration", 27000, "s"))
    for item in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(item)
    corrected = views.selected_metric_status(runtime.engine, "sleep.duration", window)
    assert corrected["observations"][-1]["value"] == 27000
    assert corrected["version"] != first["version"]
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.update()
            .where(dataset_state.c.source == "garmin", dataset_state.c.dataset == "test")
            .values(last_error="synthetic fetch failure")
        )
    failed = views.selected_metric_status(runtime.engine, "sleep.duration", window)
    assert failed["freshness"] == "unavailable"
    assert failed["problem"]["stage"] == "fetch"
    assert failed["observations"][-1]["value"] == 27000


def test_dashboard_metric_status_is_private_and_uses_shared_projection(runtime):
    route = next(
        r.endpoint for r in build_router(runtime).routes if r.path == "/dashboard-metric-status"
    )
    today = today_in(runtime.settings.timezone)
    put(runtime, "garmin", DailyMetric(today, "sleep.duration", 25000, "s"))
    result = route()["data"]["sleep.duration"]
    expected = views.selected_metric_status(
        runtime.engine, "sleep.duration", Window(today - timedelta(days=13), today)
    )
    assert result == expected
    share = Share(id="synthetic", title="synthetic", pages=("dashboard",), can_write=False)
    assert not share.allows("GET", "/dashboard-metric-status")
    request = Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": []})
    dashboard = next(r.endpoint for r in build_router(runtime).routes if r.path == "/dashboard")
    assert "coaching_state" not in dashboard(request)["data"]


def test_dashboard_load_performs_one_capture_via_activity_assessment(runtime, monkeypatch):
    from test_evidence import activity

    from vibesynergy.query import assessments, coach

    coach.save_draft(runtime.engine, "policy", {}, expected_version=0, reason="test")
    coach.activate(runtime.engine, "policy", expected_version=1, reason="test")
    put(runtime, "garmin", activity())
    captures = []
    original = assessments._capture
    monkeypatch.setattr(
        assessments, "_capture", lambda *a, **k: captures.append(1) or original(*a, **k)
    )
    routes = {(r.path, m): r.endpoint for r in build_router(runtime).routes for m in r.methods}
    request = Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": []})
    routes["/dashboard", "GET"](request)
    assert captures == []  # the blocking dashboard response does no coaching capture
    state = routes["/activity-assessment", "GET"]("garmin:1")["data"]["coaching_state"]
    assert state["activity_id"] == "garmin:1"
    assert state["reason"] == "retired"
    assert captures == []
