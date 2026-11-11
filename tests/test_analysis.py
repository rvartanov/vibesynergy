"""Analysis and training queries against planted data with known answers."""

from __future__ import annotations

import math
import os
from datetime import UTC, date, datetime, timedelta
from itertools import count

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, DailyMetric, Stream, WellnessSamples
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import analysis, training
from vibesynergy.store.parquet import ParquetStore

START = date(2026, 1, 1)
_ids = count()


def _put(rt, source: str, *records) -> None:
    stored = rt.raw.put(
        source=source,
        dataset="t",
        item_key=str(next(_ids)),
        payload=str(next(_ids)).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _days(n: int):
    return [START + timedelta(days=i) for i in range(n)]


def test_trend_recovers_planted_slope(runtime) -> None:
    # weight falls 0.1 kg/day with a little alternating noise
    _put(
        runtime,
        "withings",
        *[
            DailyMetric(d, "body.weight", 80 - 0.1 * i + (0.05 if i % 2 else -0.05), "kg")
            for i, d in enumerate(_days(60))
        ],
    )
    unify.rebuild(runtime.engine)
    for method in ("theil_sen", "linear"):
        t = analysis.trend(
            runtime.engine, "body.weight", Window(START, START + timedelta(59)), method=method
        )
        assert t["slope_per_week"] == pytest.approx(-0.7, abs=0.02)
        lo, hi = t["slope_per_week_ci95"]
        assert lo <= -0.7 <= hi and t["direction"] == "falling"


def test_compare_periods(runtime) -> None:
    _put(
        runtime,
        "garmin",
        *[
            DailyMetric(d, "hr.resting", 50 if i < 28 else 45, "bpm")
            for i, d in enumerate(_days(56))
        ],
    )
    unify.rebuild(runtime.engine)
    later = Window(START + timedelta(28), START + timedelta(55))
    out = analysis.compare(
        runtime.engine, ["hr.resting"], later, analysis.previous_window(later, "previous")
    )["hr.resting"]
    assert out["difference"] == -5 and out["mann_whitney_p"] < 0.001


def test_correlation_finds_the_lag(runtime) -> None:
    loads = [float((i * 37) % 100) for i in range(90)]
    _put(
        runtime,
        "garmin",
        *[DailyMetric(d, "load.x", loads[i], "load") for i, d in enumerate(_days(90))],
    )
    # HRV reacts to load one day later
    _put(
        runtime,
        "garmin",
        *[
            DailyMetric(d, "hrv.night_avg", 80 - 0.3 * loads[i - 1], "ms")
            for i, d in enumerate(_days(90))
            if i > 0
        ],
    )
    unify.rebuild(runtime.engine)
    out = analysis.correlate(
        runtime.engine,
        "load.x",
        "hrv.night_avg",
        Window(START, START + timedelta(88)),
        lags=range(0, 3),
    )
    by_lag = {r["lag_days"]: r["r"] for r in out["by_lag"]}
    assert by_lag[1] == pytest.approx(-1.0) and abs(by_lag[0]) < 0.5


def test_anomalies_and_distribution(runtime) -> None:
    values = [60.0 + (i % 3) for i in range(60)]
    values[50] = 30.0  # a bad night
    _put(
        runtime,
        "garmin",
        *[DailyMetric(d, "hrv.night_avg", values[i], "ms") for i, d in enumerate(_days(60))],
    )
    unify.rebuild(runtime.engine)
    window = Window(START + timedelta(30), START + timedelta(59))
    found = analysis.anomalies(runtime.engine, ["hrv.night_avg"], window)
    assert [(a["date"], a["direction"]) for a in found] == [(str(START + timedelta(50)), "low")]
    dist = analysis.distribution(runtime.engine, "hrv.night_avg", window, bins=5)
    assert dist["n"] == 30 and sum(b["count"] for b in dist["histogram"]) == 30


def test_training_load_matches_ewma(runtime) -> None:
    base = datetime(2026, 1, 1, 7, tzinfo=UTC)
    _put(
        runtime,
        "garmin",
        *[
            Activity(
                str(i),
                base + timedelta(days=i),
                START + timedelta(i),
                "running",
                moving_s=3600.0,
                training_load=100.0,
            )
            for i in range(30)
        ],
    )
    unify.rebuild(runtime.engine)
    out = training.training_load(
        runtime.engine, Window(START, START + timedelta(29)), load="garmin"
    )
    k = 1 - math.exp(-1 / 42)
    expected_ctl = 100 * (1 - (1 - k) ** 30)
    assert out["current"]["ctl"] == pytest.approx(expected_ctl, abs=0.1)
    assert out["current"]["atl"] > out["current"]["ctl"] and out["current"]["tsb"] < 0
    vol = training.volume(runtime.engine, Window(START, START + timedelta(29)), granularity="month")
    assert vol["totals"]["running"]["count"] == 30


def test_power_curve_from_streams(runtime) -> None:
    t0 = datetime(2026, 3, 1, 8, tzinfo=UTC)
    watts = [200.0] * 600 + [400.0] * 60 + [150.0] * 600  # a 1-minute effort at 400 W
    stream = Stream(
        "fit-P", {"t": [t0 + timedelta(seconds=i) for i in range(len(watts))], "power": watts}
    )
    runtime.parquet.write_stream(stream)
    _put(
        runtime,
        "files",
        Activity(
            "fit-P", t0, date(2026, 3, 1), "cycling", fit_id="fit-P", moving_s=float(len(watts))
        ),
    )
    unify.rebuild(runtime.engine)
    curve = training.power_curve(runtime.engine, runtime.parquet, Window(START, date(2026, 12, 31)))
    best = {p["seconds"]: p["watts"] for p in curve["curve"]}
    assert best[60] == 400 and best[5] == 400
    assert best[600] == pytest.approx((540 * 200 + 60 * 400) / 600)
    assert curve["activities_with_power"] == 1


def test_power_curve_keeps_running_power_out_of_cycling(runtime) -> None:
    t0 = datetime(2026, 3, 2, 8, tzinfo=UTC)
    for key, sport, watts in (
        ("fit-R", "running", 500.0),  # running power is higher and must not count
        ("fit-Z", "virtual_ride", 300.0),  # Zwift is cycling
        ("fit-B", "road_biking", 250.0),
    ):
        stream = Stream(
            key, {"t": [t0 + timedelta(seconds=i) for i in range(120)], "power": [watts] * 120}
        )
        runtime.parquet.write_stream(stream)
        _put(
            runtime, "files", Activity(key, t0, date(2026, 3, 2), sport, fit_id=key, moving_s=120.0)
        )
        t0 += timedelta(hours=3)
    unify.rebuild(runtime.engine)
    span = Window(START, date(2026, 12, 31))
    for sport in ("cycl", "ride"):
        curve = training.power_curve(runtime.engine, runtime.parquet, span, sport=sport)
        assert curve["activities_with_power"] == 2, sport
        assert {p["seconds"]: p["watts"] for p in curve["curve"]}[60] == 300
    run = training.power_curve(runtime.engine, runtime.parquet, span, sport="run")
    assert run["activities_with_power"] == 1


def test_intraday_local_day_and_dedup(runtime) -> None:
    day = date(2026, 6, 1)
    ts = [datetime(2026, 5, 31, 22, 0, tzinfo=UTC) + timedelta(minutes=i) for i in range(10)]
    samples = WellnessSamples("k1", ts, ["hr.sample"] * 10, [60.0 + i for i in range(10)])
    runtime.parquet.write_wellness("garmin", samples)
    runtime.parquet.write_wellness("garmin_export", samples)  # the same file twice
    out = training.intraday(runtime.parquet, day, "Europe/Amsterdam", resample_min=5)
    assert out["columns"] == ["time", "hr.sample"]
    assert out["rows"] == [["00:00:00", 62.0], ["00:05:00", 67.0]]  # local midnight, not duplicated


def test_a_day_reads_only_its_own_wellness_files(runtime) -> None:
    store = runtime.parquet
    t0 = datetime(2026, 6, 1, 8, tzinfo=UTC)
    for n in range(5):  # one file per day
        ts = [t0 + timedelta(days=n, minutes=i) for i in range(3)]
        store.write_wellness("garmin", WellnessSamples(f"d{n}", ts, ["hr.sample"] * 3, [60.0] * 3))
    day2 = (t0 + timedelta(days=2)).replace(hour=0)
    assert [p.name for p in store.wellness_files(day2, day2 + timedelta(days=1))] == ["d2.parquet"]
    assert (store.root / "wellness" / "_index.json").exists()
    # A fresh store (a restart) reads the index; a rewritten file is looked at again.
    fresh = ParquetStore(store.root)
    moved = [t0 + timedelta(days=3, minutes=i) for i in range(3)]
    rewritten = fresh.write_wellness(
        "garmin", WellnessSamples("d0", moved, ["hr.sample"] * 3, [70.0] * 3)
    )
    # Same-size rewrites can keep the previous mtime on a coarse clock, which the index
    # treats as unchanged. Move the stamp so this asserts the rewrite is noticed.
    stamped = rewritten.stat().st_mtime_ns + 10_000_000
    os.utime(rewritten, ns=(stamped, stamped))
    fresh._index_stamp = None  # as if the folder's time had moved on
    day3 = day2 + timedelta(days=1)
    names = sorted(p.name for p in fresh.wellness_files(day3, day3 + timedelta(days=1)))
    assert names == ["d0.parquet", "d3.parquet"]
    none = fresh.wellness_files(datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 1, 2, tzinfo=UTC))
    assert none == []


def test_cadence_in_steps_per_minute_on_foot(runtime) -> None:
    from vibesynergy.query import views

    t0 = datetime(2026, 3, 2, 8, tzinfo=UTC)
    ts = [t0 + timedelta(seconds=i) for i in range(10)]
    for fit_id, sport in (("fit-R", "running"), ("fit-C", "cycling")):
        runtime.parquet.write_stream(Stream(fit_id, {"t": ts, "cadence": [85.0] * 10}))
        _put(runtime, "files", Activity(fit_id, t0, date(2026, 3, 2), sport, fit_id=fit_id))
        t0 += timedelta(hours=2)
    unify.rebuild(runtime.engine)
    run = views.activity_streams(runtime.engine, runtime.parquet, "files:fit-R")
    ride = views.activity_streams(runtime.engine, runtime.parquet, "files:fit-C")
    assert set(run["columns"]["cadence"]) == {170.0}  # devices store strides per leg
    assert set(ride["columns"]["cadence"]) == {85.0}  # pedal rpm stays as is


def test_trimp_load_from_heart_rate(runtime) -> None:
    # Banister: 60 min at 60% of heart-rate reserve = 60 * 0.6 * 0.64 * e^(1.92 * 0.6)
    assert training.trimp(60, 128, 50, 180, "male") == pytest.approx(72.9, abs=0.1)
    assert training.trimp(60, 128, 50, 180, "female") == pytest.approx(84.3, abs=0.1)
    assert training.trimp(30, 40, 50, 180, None) == 0  # below resting: no load
    _put(
        runtime,
        "files",
        Activity(
            "hr-1",
            datetime(2026, 3, 3, 8, tzinfo=UTC),
            date(2026, 3, 3),
            "running",
            elapsed_s=3600.0,
            avg_hr=128.0,
        ),
    )
    unify.rebuild(runtime.engine)
    athlete = {"max_hr": 180, "resting_hr": 50, "sex": "male"}
    out = training.training_load(
        runtime.engine, Window(date(2026, 3, 3), date(2026, 3, 3)), athlete=athlete, load="trimp"
    )
    assert out["load"] == "trimp" and out["rows"][0][1] == pytest.approx(72.9, abs=0.1)
    assert out["hr_profile"]["max_from"] == "config"


def test_sleep_regularity_across_midnight() -> None:
    from vibesynergy.query import views

    def night(bed: str, wake: str) -> dict:
        return {"start_utc": f"2026-03-01T{bed}:00+00:00", "end_utc": f"2026-03-02T{wake}:00+00:00"}

    # 23:30 and 00:30 local bedtimes (UTC+1 in March) are an hour apart, not 23 hours.
    r = views.sleep_regularity(
        [night("22:30", "06:00"), night("23:30", "06:00")], "Europe/Amsterdam"
    )
    assert r["bedtime"] == {"mean": "00:00", "sd_min": 42}
    assert r["wake"] == {"mean": "07:00", "sd_min": 0}
    assert views.sleep_regularity([night("22:30", "06:00")], "Europe/Amsterdam") is None
