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
