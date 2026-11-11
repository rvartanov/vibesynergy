"""The plan's workout model: intervals.icu text to steps and back."""

from __future__ import annotations

import pytest

from vibesynergy.query.workout_model import parse_intervals, to_intervals

BIKE = """Lage cadans, zittend.

Warmup
- 15m 50-65% 90rpm

Main set
4x
- 6m 75-85% 50-60rpm
- 3m 55% 90rpm

Cooldown
- 9m 50-60% 90rpm"""

SWIM = """Rustig.

Warmup
- Licht 300mtr Z1 Pace

Main set
3x
- Middel 300mtr Z2 Pace
- 30s intensity=rest

Cooldown
- Licht 200mtr Z1 Pace"""

CLOCK = """Coachklok.

Warmup
- WU + techniek 17m press lap intensity=warmup

Main set 8x
- DL3 30s intensity=interval
- Dribbel 30s intensity=recovery

- CD uitlopen 10m press lap intensity=cooldown"""

STRENGTH = """Anatomische adaptatie.

[garmin]
warmup time=5m "Warming-up"
SQUAT/GOBLET_SQUAT sets=2 reps=15 rest=75s "Goblet squat"

- 40m free"""


@pytest.mark.parametrize("text", [BIKE, SWIM, CLOCK, STRENGTH, "- 20m Z2 HR", "- 1m40s Z1 HR"])
def test_parse_and_write_back_keep_the_workout(text: str) -> None:
    first = parse_intervals(text)
    assert first.unparsed == []
    again = parse_intervals(to_intervals(first))
    assert (again.steps, again.intro, again.garmin_block) == (
        first.steps,
        first.intro,
        first.garmin_block,
    )


def test_steps_carry_relative_targets_rest_and_lap() -> None:
    bike = parse_intervals(BIKE)
    work = bike.steps[1]["steps"][0]
    assert bike.steps[1]["times"] == 4
    assert work["target"] == {"type": "pct_ftp", "low": 75, "high": 85}
    assert work["cadence"] == [50, 60]
    swim = parse_intervals(SWIM)
    assert swim.steps[1]["steps"][1] == {"kind": "rest", "duration_s": 30}
    assert swim.steps[0]["target"] == {"type": "pace_zone", "low": 1, "high": 1}
    clock = parse_intervals(CLOCK)
    assert clock.steps[0]["until_lap"] and clock.steps[0]["kind"] == "warmup"
    assert clock.steps[-1]["kind"] == "cooldown"
    strength = parse_intervals(STRENGTH)
    assert strength.garmin_block.startswith("[garmin]")
    assert strength.steps == [{"kind": "work", "duration_s": 2400, "target": {"type": "none"}}]


def test_unknown_syntax_is_reported_not_guessed() -> None:
    out = parse_intervals("- 10m 200w")
    assert out.unparsed == ["- 10m 200w"] and out.steps == []
