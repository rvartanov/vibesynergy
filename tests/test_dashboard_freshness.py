"""The dashboard does no coaching capture; the activity assessment read reports it retired."""

from starlette.requests import Request
from test_evidence import put

from vibesynergy.portal.api import build_router


def test_dashboard_load_performs_no_capture_and_the_assessment_read_is_retired(
    runtime, monkeypatch
):
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
    data = routes["/dashboard", "GET"](request)["data"]
    assert captures == []  # the blocking dashboard response does no coaching capture
    assert "coaching_state" not in data
    state = assessments.read(runtime, "garmin:1")["coaching_state"]
    assert state["activity_id"] == "garmin:1"
    assert state["reason"] == "retired"
    assert captures == []
