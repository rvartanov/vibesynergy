"""A window of an activity, and similar activities, on synthetic streams with known answers."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.derive import unify
from vibesynergy.model import Activity, ActivityLap, Stream, ZoneSet
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import activity_analysis as aa

_ids = itertools.count(1)
T0 = datetime(2026, 6, 1, 8, tzinfo=UTC)


def _put(rt, *records) -> None:
    stored = rt.raw.put(
        source="files",
        dataset="t",
        item_key=str(next(_ids)),
        payload=str(next(_ids)).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source="files", raw_item_id=stored.id)


def _ride(rt, fit_id: str, day: int = 0, seconds: int = 7200, **extra) -> None:
    """Two hours at 1 Hz: 200 W / 130 bpm the first hour, 250 W / 150 bpm the second,
    8 m/s throughout, one 50 m climb."""
    start = T0 + timedelta(days=day)
    ts = [start + timedelta(seconds=i) for i in range(seconds)]
    first = [i < seconds // 2 for i in range(seconds)]
    cols = {
        "t": ts,
        "power": [200.0 if f else 250.0 for f in first],
        "hr": [130.0 if f else 150.0 for f in first],
        "speed": [8.0] * seconds,
        "dist": [8.0 * i for i in range(seconds)],
        "alt": [min(i, 500) / 10 for i in range(seconds)],
        "cadence": [90.0] * seconds,
        **extra,
    }
    rt.parquet.write_stream(Stream(fit_id, cols))
    act = Activity(
        fit_id,
        start,
        start.date(),
        "cycling",
        name=f"Ride {fit_id}",
        elapsed_s=seconds,
        moving_s=seconds,
        distance_m=8.0 * seconds,
        avg_hr=140.0,
        fit_id=fit_id,
    )
    _put(rt, act)


def test_the_last_hour_against_the_first(runtime) -> None:
    _ride(runtime, "r1")
    zones = ZoneSet("hr", "cycling", date(2026, 1, 1), (100.0, 120.0, 140.0, 160.0, 180.0, 190.0))
    _put(runtime, zones)
    unify.rebuild(runtime.engine)
    out = aa.analyze(runtime.engine, runtime.parquet, "files:r1", last_minutes=60)
    w, rest = out["window"], out["rest_of_activity"]
    assert (w["from_s"], w["avg_power"], w["avg_hr"]) == (3599.0, 250.0, 150.0)
    assert w["norm_power"] == pytest.approx(250, abs=0.5)
    assert rest["avg_power"] == pytest.approx(200, abs=0.2)
    assert out["window_minus_rest"]["avg_power"] == pytest.approx(50, abs=0.2)
    assert w["distance_m"] == pytest.approx(8 * 3600, abs=10)
    assert "pace_s_per_km" not in w
    assert w["sport_units"]["pace_or_speed"] == "28.8 km/h"
    assert w["time_in_hr_zones_s"]["3"] == pytest.approx(3600, abs=2)  # 150 bpm: zone 3

    whole = aa.analyze(runtime.engine, runtime.parquet, "files:r1")["window"]
    assert whole["hr_drift_bpm"] == pytest.approx(20, abs=0.1)
    # EF from 200/130 to 250/150: efficiency rose, so decoupling is negative (~ -8.3%).
    assert whole["decoupling_pct"] == pytest.approx(
        100 * (200 / 130 - 250 / 150) / (200 / 130), abs=0.3
    )
    assert whole["ascent_m"] == pytest.approx(50, abs=1.5)
    assert whole["avg_cadence"] == 90

    by_time = aa.analyze(runtime.engine, runtime.parquet, "files:r1", start="0:30:00", end="45min")
    assert (by_time["window"]["seconds"], by_time["window"]["avg_power"]) == (899, 200)
    by_distance = aa.analyze(runtime.engine, runtime.parquet, "files:r1", start="40km")
    assert by_distance["window"]["from_s"] == 5000
    for bad in ({"start": "sometime"}, {"start": "2h", "end": "1h"}, {"start": "900km"}):
        with pytest.raises(ValueError):
            aa.analyze(runtime.engine, runtime.parquet, "files:r1", **bad)


def test_normalized_power_weights_surges() -> None:
    steady = aa._normalized_power([200.0] * 600, 1)
    surges = aa._normalized_power(([100.0] * 60 + [300.0] * 60) * 5, 1)
    assert steady == 200 and surges > 210  # same average, harder effort


def test_similar_by_profile_route_and_workout(runtime) -> None:
    loop = [(52.2 + i / 1000, 5.8 + (i % 20) / 2000) for i in range(300)]
    other = [(52.5 + i / 1000, 5.3) for i in range(300)]

    def track(points):
        return {"lat": [p[0] for p in points], "lon": [p[1] for p in points]}

    _ride(runtime, "a", day=0, seconds=300, **track(loop))
    _ride(runtime, "b", day=7, seconds=300, **track(loop))  # the same loop a week later
    _ride(runtime, "c", day=8, seconds=300, **track(other))  # as long, elsewhere
    _ride(runtime, "d", day=9, seconds=60)  # much shorter
    for fit_id, laps in (("a", (60, 30, 60, 30, 60)), ("c", (60, 30, 60, 30, 60))):
        for i, seconds in enumerate(laps):
            power = 300.0 if i % 2 == 0 else 120.0
            _put(runtime, ActivityLap(fit_id, i, moving_s=seconds, avg_power=power))
    unify.rebuild(runtime.engine)

    profile = aa.similar(runtime.engine, runtime.parquet, "files:a", by="profile")
    ids = [m["id"] for m in profile["matches"]]
    assert set(ids[:2]) == {"files:b", "files:c"} and "files:d" not in ids
    route = aa.similar(runtime.engine, runtime.parquet, "files:a", by="route")
    assert [m["id"] for m in route["matches"]] == ["files:b"]
    assert route["matches"][0]["score"] == pytest.approx(1, abs=0.01)
    workout = aa.similar(runtime.engine, runtime.parquet, "files:a", by="workout")
    assert [m["id"] for m in workout["matches"]] == ["files:c"]
    with pytest.raises(ValueError):
        aa.similar(runtime.engine, runtime.parquet, "files:d", by="workout")  # steady, no laps
    with pytest.raises(ValueError):
        aa.similar(runtime.engine, runtime.parquet, "files:a", by="shape")
