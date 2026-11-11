"""Cross-source dedup and best-source rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import count

from sqlalchemy import select

from vibesynergy.derive import unify
from vibesynergy.model import Activity, DailyMetric, FitFile
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.store.schema import activity_link, activity_unified, daily_unified

T = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
DAY = date(2026, 9, 20)
_ids = count()


def _put(rt, source: str, *records) -> None:
    stored = rt.raw.put(
        source=source,
        dataset="test",
        item_key=str(next(_ids)),
        payload=str(next(_ids)).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _act(source_id: str, start: datetime = T, **kw) -> Activity:
    base = {"sport": "cycling", "elapsed_s": 3600.0, "distance_m": 30000.0, "name": "Ride"}
    return Activity(source_id, start, DAY, **{**base, **kw})


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def test_rebuild_retries_linking_when_activities_change_during_link(runtime, monkeypatch) -> None:
    _put(runtime, "garmin", _act("1", name="Morning Ride"))
    unify.rebuild(runtime.engine)
    original = unify.link_activities
    seen = {"passes": 0}

    def link_with_insert(rows, fits):
        seen["passes"] += 1
        if seen["passes"] == 1:
            _put(runtime, "garmin", _act("2", start=T + timedelta(hours=2), name="Afternoon Ride"))
        return original(rows, fits)

    monkeypatch.setattr(unify, "link_activities", link_with_insert)
    unify.rebuild(runtime.engine)
    rows = _rows(runtime, activity_unified)
    assert len(rows) == 2
    assert seen["passes"] >= 2


def test_rebuild_relinks_inside_write_lock_when_fingerprint_moves_late(
    runtime, monkeypatch
) -> None:
    """A change after the read-side checks still lands in the written snapshot."""
    _put(runtime, "garmin", _act("1", name="Morning Ride"))
    real = unify._activity_fingerprint
    seen = {"n": 0, "before": None}

    def fingerprint(conn):
        seen["n"] += 1
        value = real(conn)
        if seen["n"] == 1:
            seen["before"] = value
            return value
        if seen["n"] == 2:
            # A committed activity row, not a nested rebuild: the read-side snapshot is stale.
            stored = runtime.raw.put(
                source="garmin",
                dataset="test",
                item_key="late",
                payload=b"late",
                content_type="text/plain",
            )
            with runtime.engine.begin() as write:
                write_records(
                    write,
                    [_act("2", start=T + timedelta(hours=3), name="Evening Ride")],
                    source="garmin",
                    raw_item_id=stored.id,
                )
            return seen["before"]
        return real(conn)

    monkeypatch.setattr(unify, "_activity_fingerprint", fingerprint)
    unify.rebuild(runtime.engine)
    rows = _rows(runtime, activity_unified)
    assert len(rows) == 2
    assert {row["name"] for row in rows} == {"Morning Ride", "Evening Ride"}


def test_rebuild_results_unchanged_when_write_phase_is_short(runtime) -> None:
    _put(runtime, "garmin", _act("1", name="Morning Ride", avg_power=210.0))
    _put(runtime, "garmin_export", _act("1", name="Morning Ride", calories=800.0))
    unify.rebuild(runtime.engine)
    before = _rows(runtime, activity_unified)
    links_before = _rows(runtime, activity_link)
    unify.rebuild(runtime.engine)
    after = _rows(runtime, activity_unified)
    links_after = _rows(runtime, activity_link)

    def without_updated(rows):
        return [{k: v for k, v in row.items() if k != "updated_at"} for row in rows]

    assert without_updated(before) == without_updated(after)
    assert links_before == links_after


def test_one_ride_from_four_sources(runtime) -> None:
    _put(runtime, "garmin", _act("1", name="Morning Ride", avg_power=210.0))
    _put(runtime, "garmin_export", _act("1", name="Morning Ride", calories=800.0))
    _put(runtime, "garmin", FitFile("fit-A", "activity", source_activity_id="1"))
    _put(runtime, "files", _act("fit-A", name=None, fit_id="fit-A"))
    _put(
        runtime,
        "strava",
        _act(
            "S1",
            start=T + timedelta(minutes=30),
            name="Ride with Anna",
            fit_id="strava-S1",
            extra={"external_id": "garmin_ping_1"},
        ),
    )

    stats = unify.rebuild(runtime.engine)
    assert (stats.activities, stats.groups) == (4, 1)
    (ride,) = _rows(runtime, activity_unified)
    assert ride["group_id"] == "garmin:1" and ride["primary_source"] == "garmin"
    assert ride["avg_power"] == 210 and ride["calories"] == 800  # gaps filled from the export
    assert ride["name"] == "Ride with Anna"  # edited on Strava
    assert ride["stream_key"] == "fit-A"  # the original FIT beats Strava streams
    rules = {(r["source"], r["source_id"]): r["rule"] for r in _rows(runtime, activity_link)}
    assert rules == {
        ("garmin", "1"): "primary",
        ("garmin_export", "1"): "same_id",
        ("files", "fit-A"): "fit_file",
        ("strava", "S1"): "external_id",
    }


def test_time_matching(runtime) -> None:
    _put(runtime, "garmin", _act("2"))
    _put(
        runtime,
        "withings",
        _act("W", start=T + timedelta(seconds=5), elapsed_s=3500.0, distance_m=None),
    )
    _put(runtime, "strava", _act("far", start=T + timedelta(seconds=30), distance_m=60000.0))
    _put(runtime, "garmin", _act("3", start=T + timedelta(seconds=10)))  # same source: never
    unify.rebuild(runtime.engine)
    groups = {(r["source"], r["source_id"]): r["group_id"] for r in _rows(runtime, activity_link)}
    assert groups[("withings", "W")] in {"garmin:2", "garmin:3"}
    assert groups[("strava", "far")] == "strava:far"  # double the distance: another session
    assert groups[("garmin", "2")] != groups[("garmin", "3")]


def test_overlap_matching(runtime) -> None:
    # A trainer app uploads to Strava while the watch records: starts 2.5 min apart, the
    # distances 7% apart, but the same half hour.
    _put(runtime, "garmin", _act("5", elapsed_s=2105.0, distance_m=16366.0))
    _put(
        runtime,
        "strava",
        _act("S5", start=T + timedelta(seconds=144), elapsed_s=1991.0, distance_m=15243.0),
    )
    # Overlapping only half of the longer one: a different session.
    _put(runtime, "withings", _act("W5", start=T + timedelta(minutes=10), elapsed_s=4000.0))
    unify.rebuild(runtime.engine)
    groups = {(r["source"], r["source_id"]): r["group_id"] for r in _rows(runtime, activity_link)}
    assert groups[("strava", "S5")] == "garmin:5"
    assert groups[("withings", "W5")] == "withings:W5"


def test_strava_filename_links_garmin_without_external_id(runtime) -> None:
    _put(runtime, "garmin", _act("99", name="Evening Ride"))
    _put(
        runtime,
        "strava",
        _act(
            "S99",
            start=T + timedelta(seconds=30),
            name="Evening Ride",
            extra={"filename": "99.fit", "external_id": ""},
        ),
    )
    unify.rebuild(runtime.engine)
    (row,) = _rows(runtime, activity_unified)
    assert row["sources"] == ["garmin:99", "strava:S99"]
    rules = {(r["source"], r["source_id"]): r["rule"] for r in _rows(runtime, activity_link)}
    assert rules[("strava", "S99")] == "filename"


def test_strava_filename_missing_garmin_stays_unlinked(runtime) -> None:
    _put(
        runtime,
        "strava",
        _act("Sx", extra={"filename": "404404.fit", "external_id": ""}),
    )
    unify.rebuild(runtime.engine)
    (row,) = _rows(runtime, activity_unified)
    assert row["group_id"] == "strava:Sx"
    assert len(_rows(runtime, activity_link)) == 1


def test_strava_without_filename_still_links_by_time(runtime) -> None:
    _put(runtime, "garmin", _act("7", elapsed_s=3600.0, distance_m=30000.0))
    _put(
        runtime,
        "strava",
        _act(
            "S7",
            start=T + timedelta(seconds=45),
            elapsed_s=3580.0,
            distance_m=29900.0,
            extra={},
        ),
    )
    unify.rebuild(runtime.engine)
    groups = {(r["source"], r["source_id"]): r["group_id"] for r in _rows(runtime, activity_link)}
    assert groups[("strava", "S7")] == "garmin:7"


def test_same_strava_and_garmin_name_keeps_primary(runtime) -> None:
    _put(runtime, "garmin", _act("4", name="Lunch Run"))
    _put(runtime, "strava", _act("S4", name="Lunch Run", extra={"external_id": "garmin_push_4"}))
    unify.rebuild(runtime.engine)
    (row,) = _rows(runtime, activity_unified)
    assert row["name"] == "Lunch Run" and row["sources"] == ["garmin:4", "strava:S4"]


def test_best_source_per_metric(runtime) -> None:
    _put(
        runtime,
        "garmin",
        DailyMetric(DAY, "body.weight", 80.0, "kg"),
        DailyMetric(DAY, "sleep.score", 80.0, "score"),
    )
    _put(
        runtime,
        "withings",
        DailyMetric(DAY, "body.weight", 79.0, "kg"),
        DailyMetric(DAY, "sleep.score", 85.0, "score"),
        DailyMetric(DAY, "daily.steps", 5000.0, "count"),
    )
    _put(runtime, "strava", DailyMetric(DAY, "body.weight", 70.0, "kg"))  # unlisted: last

    unify.rebuild(runtime.engine)
    best = {r["metric"]: (r["source"], r["value"]) for r in _rows(runtime, daily_unified)}
    assert best == {
        "body.weight": ("withings", 79.0),
        "sleep.score": ("garmin", 80.0),
        "daily.steps": ("withings", 5000.0),
    }

    unify.rebuild(runtime.engine, {"sleep.": ["withings"]})  # configurable
    best = {r["metric"]: r["source"] for r in _rows(runtime, daily_unified)}
    assert best["sleep.score"] == "withings"


def test_underscore_in_prefix_is_literal(runtime) -> None:
    _put(runtime, "garmin", DailyMetric(DAY, "hrvXnight", 1.0, "ms"))
    _put(runtime, "withings", DailyMetric(DAY, "hrvXnight", 2.0, "ms"))
    unify.rebuild(runtime.engine, {"hrv_": ["withings"]})  # must not match "hrvX…"
    (row,) = _rows(runtime, daily_unified)
    assert row["source"] == "garmin"


def test_derive_runs_after_sync(runtime) -> None:
    runtime.sync.sync("demo", ["daily"])
    assert _rows(runtime, daily_unified)  # demo metrics appear without a manual derive


def test_source_order_longest_prefix() -> None:
    assert unify.source_order("body.weight")[0] == "withings"
    assert unify.source_order("sleep.score")[0] == "garmin"
    assert unify.source_order("body.weight", {"body.weight": ["garmin"]}) == ["garmin"]


def test_a_trainer_app_ride_merges_with_the_watch_ride(runtime) -> None:
    """MyWhoosh/Zwift upload their own copy of an indoor ride (#11): the virtual distance
    differs from the watch's, and a shorter app session can lie inside a longer ride that
    Strava also has from Garmin. One session, one activity."""
    start = T
    _put(
        runtime,
        "garmin",
        _act("g1", start=start, sport="indoor_cycling", elapsed_s=2097.0, distance_m=17276.0),
    )
    _put(
        runtime,
        "strava",
        _act(
            "s1",
            start=start + timedelta(seconds=14),
            sport="VirtualRide",
            elapsed_s=2086.0,
            distance_m=10733.0,
        ),
    )
    # A long watch ride, on Strava from Garmin too, with an app hour inside it.
    later = T + timedelta(hours=4)
    _put(
        runtime,
        "garmin",
        _act("g2", start=later, sport="indoor_cycling", elapsed_s=9068.0, distance_m=73788.0),
    )
    _put(
        runtime,
        "strava",
        _act("s2", start=later, sport="Ride", elapsed_s=9069.0, distance_m=73784.0),
    )
    _put(
        runtime,
        "strava",
        _act(
            "s3",
            start=later + timedelta(minutes=88),
            sport="VirtualRide",
            elapsed_s=3761.0,
            distance_m=28372.0,
        ),
    )
    unify.rebuild(runtime.engine)
    rows = sorted(_rows(runtime, activity_unified), key=lambda r: r["start_utc"])
    assert len(rows) == 2
    assert sorted(rows[0]["sources"]) == ["garmin:g1", "strava:s1"]
    assert sorted(rows[1]["sources"]) == ["garmin:g2", "strava:s2", "strava:s3"]


def test_outdoor_rides_still_need_a_matching_distance_and_runs_never_merge_into_rides(
    runtime,
) -> None:
    _put(runtime, "garmin", _act("g1", elapsed_s=3600.0, distance_m=30000.0))
    _put(
        runtime,
        "strava",
        _act("s1", start=T + timedelta(seconds=20), elapsed_s=3590.0, distance_m=20000.0),
    )
    # A treadmill run during an indoor ride's hour is still its own session.
    _put(
        runtime,
        "garmin",
        _act("g2", start=T + timedelta(hours=3), sport="indoor_cycling", elapsed_s=3600.0),
    )
    _put(
        runtime,
        "strava",
        _act(
            "s2",
            start=T + timedelta(hours=3, minutes=10),
            sport="VirtualRun",
            elapsed_s=1200.0,
            distance_m=4000.0,
        ),
    )
    unify.rebuild(runtime.engine)
    assert len(_rows(runtime, activity_unified)) == 4
