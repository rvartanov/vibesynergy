import pytest

from vibesynergy.query import own_load


def test_power_load_one_hour_at_ftp_is_100():
    assert own_load.power_load(3600, 265, 265) == pytest.approx(100)
    assert own_load.power_load(1800, 0.8 * 265, 265) == pytest.approx(32)


def test_pace_load_exponents():
    assert own_load.pace_load(3600, 1.0, 1.0, 3) == pytest.approx(100)
    assert own_load.pace_load(3600, 0.9, 1.0, 3) == pytest.approx(72.9)
    assert own_load.pace_load(3600, 0.9, 1.0, 2) == pytest.approx(81)


def test_hr_load_one_hour_at_lthr_is_100_and_rises_with_hr():
    assert own_load.hr_load(3600, 162, 162, 45, 180) == pytest.approx(100)
    assert own_load.hr_load(3600, 130, 162, 45, 180) < 100
    assert own_load.hr_load(3600, 40, 162, 45, 180) == 0


def test_from_icu_picks_power_for_rides_then_heart_rate_then_pace():
    ride = {
        "type": "VirtualRide",
        "moving_time": 3600,
        "icu_weighted_avg_watts": 265,
        "icu_ftp": 265,
        "average_heartrate": 140,
        "lthr": 150,
    }
    assert own_load.from_icu(ride)[0] == "power"
    swim = {
        "type": "Swim",
        "moving_time": 3600,
        "average_heartrate": 130,
        "lthr": 162,
        "pace": 0.7,
        "threshold_pace": 0.74,
    }
    assert own_load.from_icu(swim)[0] == "hr"
    del swim["average_heartrate"]
    kind, value = own_load.from_icu(swim)
    assert kind == "pace" and value == pytest.approx(100 * (0.7 / 0.74) ** 3)
    assert own_load.from_icu({"type": "Run"}) is None


def test_compare_reports_error_per_sport():
    item = {
        "type": "VirtualRide",
        "moving_time": 3600,
        "icu_weighted_avg_watts": 265,
        "icu_ftp": 265,
    }
    out = own_load.compare(
        [
            {**item, "icu_training_load": 100},
            {**item, "icu_training_load": 125},
            {"type": "Run", "icu_training_load": 50},
        ]
    )
    assert out == {"VirtualRide": {"n": 2, "median_pct": 10.0, "p90_pct": 20.0}}


def test_stream_load_weights_hard_minutes_and_skips_pauses():
    # One hour at LTHR, sampled every second: 100, like the average-based load.
    times = [float(i) for i in range(3601)]
    assert own_load.hr_stream_load(times, [162.0] * 3601, 162, 45, 180) == pytest.approx(
        100, rel=1e-3
    )
    # Same average HR (141), but half at 120 and half at 162: the hard half counts more.
    mixed = [120.0] * 1800 + [162.0] * 1801
    steady = own_load.hr_load(3600, 141, 162, 45, 180)
    assert own_load.hr_stream_load(times, mixed, 162, 45, 180) > steady
    # A ten-minute gap (watch paused) counts as ten seconds, not ten minutes.
    gap = own_load.hr_stream_load([0.0, 600.0], [162.0, 162.0], 162, 45, 180)
    assert gap == pytest.approx(100 * 10 / 3600)
    assert own_load.hr_stream_load([0.0], [150.0], 162, 45, 180) is None


def test_for_activity_is_none_without_an_activity_or_intervals_copy(runtime):
    assert (
        own_load.for_activity(runtime.engine, runtime.raw, runtime.parquet, "garmin:none") is None
    )
