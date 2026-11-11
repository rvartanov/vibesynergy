"""Best efforts for every sport."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import bests

DAY = date(2026, 10, 1)


def test_overview_per_sport_and_together(runtime) -> None:
    stored = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="bests",
        payload=b"{}",
        content_type="application/json",
    )
    acts = [
        Activity(
            "r1",
            datetime(2026, 10, 1, 7, tzinfo=UTC),
            DAY,
            "running",
            moving_s=3600.0,
            distance_m=10000.0,
        ),
        Activity(
            "r2",
            datetime(2026, 10, 3, 7, tzinfo=UTC),
            DAY + timedelta(days=2),
            "running",
            moving_s=7200.0,
            distance_m=18000.0,
        ),
        Activity(
            "b1",
            datetime(2026, 10, 2, 7, tzinfo=UTC),
            DAY + timedelta(days=1),
            "cycling",
            moving_s=5400.0,
            distance_m=45000.0,
        ),
        Activity(
            "s1",
            datetime(2026, 10, 4, 7, tzinfo=UTC),
            DAY + timedelta(days=3),
            "lap_swimming",
            moving_s=1500.0,
            distance_m=1000.0,
        ),
        Activity(
            "s2",
            datetime(2026, 10, 5, 7, tzinfo=UTC),
            DAY + timedelta(days=4),
            "lap_swimming",
            moving_s=600.0,
            distance_m=500.0,
        ),
    ]
    with runtime.engine.begin() as conn:
        write_records(conn, acts, source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)
    out = bests.overview(runtime.engine, runtime.parquet, Window(DAY, DAY + timedelta(days=30)))
    assert out["run"]["longest_distance"]["metres"] == 18000.0
    assert out["bike"]["longest_time"]["seconds"] == 5400.0
    # the 500 m swim is faster per 100 m but too short to count
    assert out["swim"]["fastest_session"]["s_per_100m"] == 150.0
    assert out["together"]["longest_time"]["seconds"] == 7200.0
    # 1-10 to 4-10 is one week (Monday 28-9); the swim on Monday 5-10 is the next one
    assert out["together"]["most_hours_week"]["seconds"] == 3600 + 7200 + 5400 + 1500
    only = bests.overview(runtime.engine, runtime.parquet, Window(DAY, DAY), sport="swim")
    assert set(only) == {"sport", "start", "end", "swim"}
