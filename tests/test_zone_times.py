"""Time in zones counted by VibeSynergy from the stream (12-11-2026)."""

from __future__ import annotations

from vibesynergy.query import own_load, zone_times

INPUTS = own_load.Inputs(
    ftp=250.0,
    css=None,
    threshold_pace=300.0,  # 5:00 per km
    lthr={"run": 160.0, "ride": 150.0},
    max_hr={"run": 185.0, "ride": 180.0, "other": 185.0},
    rest_hr=50.0,
)


def test_seconds_by_zone_counts_each_sample_until_the_next_and_skips_pauses() -> None:
    times = [0.0, 1.0, 61.0, 62.0, 63.0]
    values = [100.0, 150.0, 210.0, None, 150.0]
    # Zones start at 0, 121 and 161. The 60-second gap after 150 counts as MAX_GAP_S; a
    # sample without a value counts nowhere, and the last sample has no time after it.
    assert zone_times.seconds_by_zone(times, values, [0, 121, 161]) == {
        1: 1.0,
        2: zone_times.MAX_GAP_S,
        3: 1.0,
    }


def test_heart_rate_zones_are_the_watchs_of_that_day_and_below_zone_1_counts_nowhere() -> None:
    from datetime import date

    history = {
        "running": [(date(2026, 9, 25), [102.0, 138.0, 145.0, 153.0, 161.0])],
        "swimming": [(date(2026, 9, 25), [96.0, 118.0, 132.0, 141.0, 148.0])],
    }
    day = date(2026, 11, 4)
    run = zone_times.bounds("hr", "run", INPUTS, history=history, day=day)
    assert run == [102.0, 138.0, 145.0, 153.0, 161.0]
    swim = zone_times.bounds("hr", "swim", INPUTS, history=history, day=day)
    assert swim[1] == 118.0  # swimming has its own zones on the watch
    times = [float(i) for i in range(6)]
    values = [90.0, 130.0, 140.0, 165.0, 175.0, 175.0]  # 90 is below zone 1
    assert zone_times.seconds_by_zone(times, values, run) == {1: 1.0, 2: 1.0, 5: 2.0}
    # Before any history: the oldest one; without history at all: Friel's from LTHR.
    assert zone_times.bounds("hr", "run", INPUTS, history=history, day=date(2026, 1, 1)) == run
    friel = zone_times.bounds("hr", "run", INPUTS)
    assert friel[0] == 0.0 and round(friel[1]) == 136  # Z2 starts above 85 % of LTHR 160


def test_power_only_for_rides_and_pace_only_for_runs() -> None:
    power = zone_times.bounds("power", "ride", INPUTS)
    assert [round(f) for f in power] == [0, 138, 188, 225, 262, 300, 375]
    assert zone_times.bounds("power", "run", INPUTS) == []
    pace = zone_times.bounds("pace", "run", INPUTS)
    assert len(pace) == 7 and pace == sorted(pace)  # slow to fast
    assert zone_times.bounds("pace", "swim", INPUTS) == []
