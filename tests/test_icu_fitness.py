"""Fitness, fatigue and form as intervals.icu computes them: its own wellness values
where it has them, its activity load per day, its algorithm (42/7-day EWMA, same-day form,
form zones on form % of fitness) for days it has nothing for, labelled as computed."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import icu_fitness, training, views

START = date(2026, 9, 1)


def _put_raw(runtime, dataset: str, month: str, items: list[dict]) -> None:
    runtime.raw.put(
        source="intervals_icu",
        dataset=dataset,
        item_key=month,
        payload=json.dumps(items).encode(),
        content_type="application/json",
    )


def _garmin(runtime, *activities: Activity) -> None:
    stored = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="icu-fitness",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(conn, list(activities), source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)


def test_form_zones_follow_intervals_icu_on_form_percent() -> None:
    assert icu_fitness.form_zone(50, 15) == "transition"  # +30 %
    assert icu_fitness.form_zone(50, 5) == "fresh"  # +10 %
    assert icu_fitness.form_zone(50, -2) == "grey"  # -4 %
    assert icu_fitness.form_zone(50, -10) == "optimal"  # -20 %
    assert icu_fitness.form_zone(50, -20) == "high_risk"  # -40 %
    assert icu_fitness.form_zone(0, -5) is None
    assert icu_fitness.form_pct(40, -12) == -30.0


def test_step_uses_the_exponential_weights_intervals_icu_uses() -> None:
    ctl, atl = icu_fitness.step(50.0, 60.0, 100.0)
    assert ctl == pytest.approx(50 + 50 * (1 - math.exp(-1 / 42)))
    assert atl == pytest.approx(60 + 40 * (1 - math.exp(-1 / 7)))
    # The forum's rounded weights: 0.0235 for fitness, 0.1331 for fatigue.
    assert 1 - math.exp(-1 / 42) == pytest.approx(0.0235, abs=1e-4)
    assert 1 - math.exp(-1 / 7) == pytest.approx(0.1331, abs=1e-4)


def test_intervals_wellness_wins_and_later_days_continue_with_its_method(runtime) -> None:
    _put_raw(
        runtime,
        "wellness",
        "2026-09",
        [
            {"id": "2026-09-10", "ctl": 48.0, "atl": 55.0, "rampRate": 2.5, "ctlLoad": 80},
            {"id": "2026-09-11", "ctl": 47.0, "atl": 48.0, "rampRate": 2.0, "ctlLoad": 0},
            {"id": "2026-09-12", "ctl": None, "atl": None},  # no figure: computed
        ],
    )
    _put_raw(
        runtime,
        "activities",
        "2026-09",
        [
            {
                "id": "i1",
                "type": "Ride",
                "start_date_local": "2026-09-12T08:00:00",
                "icu_training_load": 90,
            },
            {
                "id": "i2",
                "type": "Run",
                "start_date_local": "2026-09-12T18:00:00",
                "icu_training_load": 30,
            },
        ],
    )
    out = training.training_load(
        runtime.engine,
        Window(date(2026, 9, 10), date(2026, 9, 13)),
        load="intervals",
        raw=runtime.raw,
    )
    rows = {row[0]: dict(zip(out["columns"], row, strict=True)) for row in out["rows"]}
    first = rows["2026-09-10"]
    assert (first["ctl"], first["atl"], first["tsb"], first["ramp"]) == (48.0, 55.0, -7.0, 2.5)
    assert first["source"] == "intervals" and first["load"] == 80
    # The 12th has no wellness: intervals.icu's activity loads (90 + 30) and its formula.
    ctl, atl = icu_fitness.step(47.0, 48.0, 120.0)
    twelfth = rows["2026-09-12"]
    assert twelfth["source"] == "computed" and twelfth["load_source"] == "intervals"
    assert twelfth["load"] == 120.0
    assert twelfth["ctl"] == pytest.approx(ctl, abs=0.05)
    assert twelfth["tsb"] == pytest.approx(ctl - atl, abs=0.1)  # same day, not yesterday
    assert out["current"]["source"] == "computed"
    assert out["source"] == "mixed"
    assert out["days_by_source"] == {"intervals": 2, "computed": 2}
    # Per sport: wellness has no split, so the run's load alone drives the computed curve.
    run = training.training_load(
        runtime.engine,
        Window(date(2026, 9, 12), date(2026, 9, 12)),
        load="intervals",
        sport="run",
        raw=runtime.raw,
        warmup_days=0,
    )
    assert run["rows"][0][1] == 30.0 and run["rows"][0][7] == "computed"


def test_without_intervals_it_falls_back_to_trimp_and_says_so(runtime) -> None:
    base = datetime(2026, 9, 1, 7, tzinfo=UTC)
    _garmin(
        runtime,
        *[
            Activity(
                f"g{i}",
                base + timedelta(days=i),
                START + timedelta(days=i),
                "running",
                moving_s=3600.0,
                avg_hr=150.0,
            )
            for i in range(10)
        ],
    )
    out = training.training_load(
        runtime.engine,
        Window(START, START + timedelta(days=9)),
        load="intervals",
        athlete={"max_hr": 190, "resting_hr": 50},
        raw=runtime.raw,
    )
    assert out["source"] == "vibesynergy" and not out["has_intervals"]
    assert {row[7] for row in out["rows"]} == {"computed"}
    assert {row[8] for row in out["rows"]} == {"trimp"}
    assert out["current"]["ctl"] > 0
    assert out["current"]["form_zone"] in {"high_risk", "optimal"}
    # The older loads still work as before.
    assert training.training_load(runtime.engine, Window(START, START), load="trimp")["load"] == (
        "trimp"
    )


def test_zone_time_comes_from_intervals_icu_when_it_has_the_activity(runtime) -> None:
    _garmin(
        runtime,
        Activity(
            "z1",
            datetime(2026, 9, 3, 7, tzinfo=UTC),
            date(2026, 9, 3),
            "cycling",
            moving_s=3600.0,
        ),
    )
    _put_raw(
        runtime,
        "activities",
        "2026-09",
        [
            {
                "id": "i9",
                "external_id": "z1",
                "type": "Ride",
                "start_date": "2026-09-03T07:00:00Z",
                "start_date_local": "2026-09-03T09:00:00",
                "icu_training_load": 60,
                "icu_hr_zone_times": [1200, 1800, 600, 0, 0, 0, 0],
                "icu_zone_times": [
                    {"id": "Z1", "secs": 900},
                    {"id": "Z2", "secs": 2100},
                    {"id": "SS", "secs": 300},
                    {"id": "Z3", "secs": 600},
                ],
            }
        ],
    )
    window = Window(date(2026, 9, 1), date(2026, 9, 7))
    hr = views.intensity_distribution(runtime.engine, window, basis="hr", raw=runtime.raw)
    assert hr["total_seconds_by_zone"] == {1: 1200.0, 2: 1800.0, 3: 600.0}
    assert hr["activities_by_zone_source"] == {"intervals": 1, "other": 0}
    power = views.intensity_distribution(runtime.engine, window, basis="power", raw=runtime.raw)
    assert power["total_seconds_by_zone"] == {1: 900.0, 2: 2100.0, 3: 600.0}
    # Without the raw archive the Garmin copy is used (here it has none).
    assert views.intensity_distribution(runtime.engine, window)["activities_with_zones"] == 0


def test_projection_runs_the_same_model_forward() -> None:
    days = {date(2026, 9, 2): 100.0}
    out = icu_fitness.project(50.0, 50.0, date(2026, 9, 1), days, date(2026, 9, 3))
    assert [row["date"] for row in out] == ["2026-09-02", "2026-09-03"]
    ctl, atl = icu_fitness.step(50.0, 50.0, 100.0)
    assert out[0]["ctl"] == round(ctl, 1) and out[0]["tsb"] == round(ctl - atl, 1)
    assert out[1]["load"] == 0.0


def test_today_brief_shows_intervals_icus_form_not_vibesynergys_own(runtime) -> None:
    """vibesynergy_today (the morning coach) and vibesynergy_training_load give the same CTL/TSB
    for a day: intervals.icu's, not a TRIMP curve."""
    from vibesynergy.query import coach_brief

    day = START + timedelta(days=9)
    base = datetime(2026, 9, 1, 7, tzinfo=UTC)
    _garmin(
        runtime,
        *[
            Activity(
                f"g{i}",
                base + timedelta(days=i),
                START + timedelta(days=i),
                "running",
                moving_s=3600.0,
                avg_hr=150.0,
            )
            for i in range(10)
        ],
    )
    _put_raw(
        runtime,
        "wellness",
        "2026-09",
        [
            {"id": str(START + timedelta(days=i)), "ctl": 60.0, "atl": 67.0, "ctlLoad": 40}
            for i in range(10)
        ],
    )
    brief = coach_brief.today_brief(runtime, day)
    tool = training.training_load(
        runtime.engine, Window(day, day), load="intervals", raw=runtime.raw
    )
    assert tool["current"]["ctl"] == 60.0
    assert (brief["load"]["ctl"], brief["load"]["tsb"]) == (60.0, -7.0)
    # ACWR comes from the same intervals.icu loads: 40 a day on the last 10 of 28 days.
    assert brief["load"]["acwr"] == round(40 / (10 * 40 / 28), 2)
