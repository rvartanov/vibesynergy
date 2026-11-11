"""Read-only activity linking audit."""

from __future__ import annotations

from datetime import timedelta

from test_unify import T, _act, _put

from vibesynergy.derive import unify
from vibesynergy.query import data_quality


def test_activity_quality_reports_unlinked_pair(runtime) -> None:
    # Same layout as test_time_matching: strict group merge leaves near-matches unlinked.
    _put(runtime, "garmin", _act("2"))
    _put(
        runtime,
        "withings",
        _act("W", start=T + timedelta(seconds=5), elapsed_s=3500.0, distance_m=None),
    )
    _put(runtime, "strava", _act("far", start=T + timedelta(seconds=30), distance_m=60000.0))
    _put(runtime, "garmin", _act("3", start=T + timedelta(seconds=10)))
    unify.rebuild(runtime.engine)
    report = data_quality.activity_quality(runtime.engine)
    assert report["same_source_groups"] == []
    assert report["unlinked_candidates"]
    assert any(
        {"withings:W", "garmin:3"} == {c["a"], c["b"]} for c in report["unlinked_candidates"]
    )


def test_activity_quality_empty_when_fully_linked(runtime) -> None:
    _put(runtime, "garmin", _act("1", name="Morning Ride", avg_power=210.0))
    _put(
        runtime,
        "strava",
        _act(
            "S1",
            start=T + timedelta(minutes=30),
            name="Ride with Anna",
            extra={"external_id": "garmin_ping_1"},
        ),
    )
    unify.rebuild(runtime.engine)
    report = data_quality.activity_quality(runtime.engine)
    assert report["unlinked_candidates"] == []
    assert report["same_source_groups"] == []
