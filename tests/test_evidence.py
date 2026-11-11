"""Evidence versions respond to changed inputs, not polling, labels or duplicate copies."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from vibesynergy.derive import unify
from vibesynergy.model import Activity, ActivityZoneTime, DailyMetric, Stream
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import views

START = datetime(2026, 9, 20, 10, tzinfo=UTC)


def put(rt, source, *records):
    raw = rt.raw.put(
        source=source,
        dataset="test",
        item_key="evidence",
        payload=repr(records).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as c:
        write_records(c, records, source=source, raw_item_id=raw.id)
    unify.rebuild(rt.engine)


def read(rt, key="garmin:1"):
    return views.activity_detail(rt.engine, rt.parquet, key)["evidence"]


def activity():
    return Activity(
        "1",
        START,
        START.date(),
        "running",
        name="Test",
        elapsed_s=600,
        moving_s=600,
        distance_m=1500,
        avg_hr=130,
        fit_id="test-evidence",
    )


def test_rebuild_and_prose_do_not_change_facts(runtime):
    a = activity()
    put(runtime, "garmin", a)
    first = read(runtime)
    unify.rebuild(runtime.engine)
    assert read(runtime)["facts_version"] == first["facts_version"]
    put(runtime, "garmin", replace(a, name="Renamed", extra={"description": "New annotation"}))
    assert read(runtime)["facts_version"] == first["facts_version"]
    put(runtime, "garmin", replace(a, distance_m=1600))
    assert read(runtime)["facts_version"] != first["facts_version"]


def test_source_aliases_do_not_duplicate_identical_facts(runtime):
    a = activity()
    put(runtime, "garmin", a)
    original = read(runtime)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    for key in ("garmin:1", "strava:2"):
        result = read(runtime, key)
        assert result["source_refs"] == ["garmin:1", "strava:2"]
        assert result["facts_version"] == original["facts_version"]


def test_zones_and_stream_content_are_versioned(runtime):
    a = activity()
    z = ActivityZoneTime("1", "hr", 1, 600, 100)
    put(runtime, "garmin", a, z)
    absent = read(runtime)
    assert absent["stream"]["state"] == "absent"
    cols = {"t": [START, START + timedelta(seconds=1)], "hr": [130.0, 140.0]}
    runtime.parquet.write_stream(Stream(a.fit_id, cols))
    first = read(runtime)
    assert first["stream"]["state"] == "available"
    assert first["facts_version"] != absent["facts_version"]
    # Same content with a different file timestamp/inode and reversed input columns.
    runtime.parquet.write_stream(Stream(a.fit_id, dict(reversed(list(cols.items())))))
    assert read(runtime)["facts_version"] == first["facts_version"]
    runtime.parquet.write_stream(Stream(a.fit_id, {**cols, "hr": [130.0, 141.0]}))
    corrected = read(runtime)
    assert corrected["facts_version"] != first["facts_version"]
    put(runtime, "garmin", a, replace(z, seconds=550))
    assert read(runtime)["facts_version"] != corrected["facts_version"]


def test_explain_reports_configured_source_precedence(runtime):
    put(runtime, "garmin", DailyMetric(START.date(), "body.weight", 75.0, "kg"))
    put(runtime, "withings", DailyMetric(START.date(), "body.weight", 76.0, "kg"))
    rules = {"body.": ["garmin", "withings"]}
    unify.rebuild(runtime.engine, rules)
    result = views.explain(runtime.engine, "body.weight", START.date(), rules)
    assert result["shown"]["source"] == "garmin"
    assert result["best_source_rule"] == {"prefix": "body.", "order": ["garmin", "withings"]}
    assert len(result["all_sources"]) == 2
