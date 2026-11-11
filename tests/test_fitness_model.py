"""Fitness, fatigue and form: the model and the own load per activity (11-11-2026)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.query import fitness_model, own_load, planned_load


def test_form_zones_on_form_percent() -> None:
    assert fitness_model.form_zone(50, 15) == "transition"  # +30 %
    assert fitness_model.form_zone(50, 5) == "fresh"  # +10 %
    assert fitness_model.form_zone(50, -2) == "grey"  # -4 %
    assert fitness_model.form_zone(50, -10) == "optimal"  # -20 %
    assert fitness_model.form_zone(50, -20) == "high_risk"  # -40 %
    assert fitness_model.form_zone(0, -5) is None
    assert fitness_model.form_pct(40, -12) == -30.0


def test_step_uses_the_42_and_7_day_exponential_weights() -> None:
    ctl, atl = fitness_model.step(50.0, 60.0, 100.0)
    assert ctl == pytest.approx(50 + 50 * (1 - math.exp(-1 / 42)))
    assert atl == pytest.approx(60 + 40 * (1 - math.exp(-1 / 7)))
    assert 1 - math.exp(-1 / 42) == pytest.approx(0.0235, abs=1e-4)
    assert 1 - math.exp(-1 / 7) == pytest.approx(0.1331, abs=1e-4)


def test_projection_runs_the_same_model_forward() -> None:
    days = {date(2026, 9, 2): 100.0}
    out = fitness_model.project(50.0, 50.0, date(2026, 9, 1), days, date(2026, 9, 3))
    assert [row["date"] for row in out] == ["2026-09-02", "2026-09-03"]
    ctl, atl = fitness_model.step(50.0, 50.0, 100.0)
    assert out[0]["ctl"] == round(ctl, 1) and out[0]["tsb"] == round(ctl - atl, 1)
    assert out[1]["load"] == 0.0


INPUTS = own_load.Inputs(
    ftp=250.0,
    css=120.0,
    threshold_pace=300.0,
    lthr={"run": 160.0, "ride": 150.0, "swim": 145.0},
    max_hr={"run": 185.0, "ride": 180.0, "swim": 180.0, "other": 185.0},
    rest_hr=50.0,
)


def _method(fam, **kw):
    base = dict(np_watts=None, avg_power=None, avg_hr=None, speed=None, has_stream=False)
    return own_load.method_for(fam, **{**base, **kw}, inputs=INPUTS)


def test_the_method_per_activity() -> None:
    assert _method("ride", np_watts=200, avg_hr=130) == "power"
    assert _method("swim", speed=0.8, avg_hr=120) == "pace"  # not wrist heart rate in water
    assert _method("run", avg_hr=150, speed=3.0, has_stream=True) == "hr_stream"
    assert _method("run", avg_hr=150) == "hr"
    assert _method("run", speed=3.0) == "pace"
    # Strength has no own max heart rate: the general one counts (11-11: it counted 0).
    assert _method("strength", avg_hr=110) == "hr"
    assert _method("other") is None


def test_the_formulas_an_hour_at_threshold_is_100() -> None:
    assert own_load.power_load(3600, 250, 250) == pytest.approx(100)
    assert own_load.pace_load(3600, 100 / 120, 100 / 120, 3) == pytest.approx(100)
    assert own_load.pace_load(3600, 1000 / 300, 1000 / 300, 2) == pytest.approx(100)
    assert own_load.hr_load(3600, 160, 160, 50, 185) == pytest.approx(100)
    # Below threshold counts less, and the exponents say how much.
    assert own_load.pace_load(3600, 0.9, 1.0, 3) == pytest.approx(72.9)
    assert own_load.pace_load(3600, 0.9, 1.0, 2) == pytest.approx(81.0)


def test_one_session_stored_twice_counts_once() -> None:
    at = datetime(2026, 10, 20, 17, tzinfo=UTC)

    def item(ident, family, offset_s, seconds, method):
        start = at + timedelta(seconds=offset_s)
        return {"id": ident, "family": family, "start": start, "seconds": seconds,
                "method": method, "load": 1}  # fmt: skip

    items = [
        item("garmin", "ride", 0, 3600, "power"),
        item("strava", "ride", 40, 3560, "hr"),  # the same ride from Strava
        item("run", "run", 0, 1800, "hr_stream"),
        item("later", "ride", 3 * 3600, 3600, "power"),
    ]
    kept = {x["id"] for x in own_load.without_duplicates(items)}
    assert kept == {"garmin", "run", "later"}


def test_planned_load_from_the_steps() -> None:
    def ftp(low, high):
        return {"type": "pct_ftp", "low": low, "high": high}

    work = {"kind": "work", "duration_s": 600, "target": ftp(100, 100)}
    steps = [
        {"kind": "warmup", "duration_s": 600, "target": ftp(50, 70)},
        {"kind": "repeat", "times": 3, "steps": [work, {"kind": "rest", "duration_s": 300}]},
    ]
    load, how = planned_load.planned_load(steps, "ride", INPUTS)
    # 10' at 60 % and 3 x 10' at 100 %: 1/6 h x 0.36 x 100 + 0.5 h x 100.
    assert how == "steps" and load == pytest.approx(600 / 3600 * 0.36 * 100 + 50)
    swim = [
        {"kind": "work", "distance_m": 400, "target": {"type": "pace_zone", "low": 4, "high": 4}}
    ]
    load, _ = planned_load.planned_load(swim, "swim", INPUTS)
    seconds = 400 / (100 / 120 * 0.98)
    assert load == pytest.approx(seconds * 0.98**3 / 36)
    # Strength: its planned time at the athlete's own rate.
    assert planned_load.planned_load([], "strength", INPUTS, seconds=3600, rate_per_hour=20) == (
        20.0,
        "rate",
    )
