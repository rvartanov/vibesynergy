"""The demo source: synthetic days and activities, deterministic and idempotent."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
from sqlalchemy import func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.sources import demo
from vibesynergy.store.schema import (
    activity,
    activity_lap,
    activity_unified,
    body_measurement,
    daily_unified,
    sleep_session,
    sleep_stage,
)

ZONE = ZoneInfo("Europe/Amsterdam")
# Monday 14 to Sunday 27 September 2026: two full weeks, long past, a ride in both.
WINDOW = Window(date(2026, 9, 14), date(2026, 9, 27))


def _count(rt, table, *where) -> int:
    with rt.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def test_the_same_day_always_gives_the_same_data() -> None:
    day = date(2026, 10, 13)  # a Tuesday: intervals
    one, two = demo.activity_payload(day, ZONE), demo.activity_payload(day, ZONE)
    assert one == two and one is not None
    assert json.dumps(demo.daily_payload(day, ZONE)) == json.dumps(demo.daily_payload(day, ZONE))
    assert demo.activity_payload(day + timedelta(days=7), ZONE) != one
    assert demo.activity_payload(date(2026, 10, 12), ZONE) is None  # Monday: rest


def test_a_week_has_runs_and_rides_with_plausible_numbers() -> None:
    sessions = [
        p
        for day in Window(date(2026, 8, 3), date(2026, 10, 25)).days()
        if (p := demo.activity_payload(day, ZONE))
    ]
    kinds = {p["kind"] for p in sessions}
    assert kinds == {"easy", "intervals", "tempo", "long", "ride"}
    assert 3 <= len(sessions) / 12 <= 5  # sessions a week
    for p in sessions:
        s, samples = p["summary"], p["samples"]
        ride = p["sport"] == "cycling"
        assert p["sport"] in ("running", "cycling")
        count = len(samples["hr"])
        assert all(len(v) == count for v in samples.values())
        assert s["elapsed_s"] == count * demo.SAMPLE_S and 35 * 60 <= s["elapsed_s"] <= 125 * 60
        assert 100 < s["avg_hr"] < 165 and s["max_hr"] < 190
        speed = s["avg_speed"] * 3.6
        assert (22 < speed < 31) if ride else (9 < speed < 13)
        assert abs(s["distance_m"] - s["avg_speed"] * s["elapsed_s"]) < 5
        assert ("power" in samples) == ride
        # The loop stays near the made-up home and ends where it started.
        assert all(abs(lat - demo.HOME[0]) < 0.25 for lat in samples["lat"])
        assert abs(samples["lat"][-1] - demo.HOME[0]) < 0.01
        # Modest raw size: one sample per ten seconds.
        assert len(json.dumps(p)) < 45_000
    # An interval session really has fast and slow parts.
    hard = next(p for p in sessions if p["kind"] == "intervals")["samples"]["speed"]
    assert max(hard) > 3.9 and min(hard[60:]) < 2.8


def test_a_session_or_night_that_has_not_ended_is_not_there_yet() -> None:
    day = date(2026, 10, 13)
    early = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
    payload = demo.daily_payload(day, ZONE, now=early)
    assert "sleep" not in payload and "hrv" not in payload and payload["steps"] > 0
    assert "sleep" in demo.daily_payload(day, ZONE, now=early + timedelta(hours=12))


def test_sync_fills_activities_streams_sleep_hrv_and_weight(runtime) -> None:
    results = {r.dataset: r for r in runtime.sync.sync("demo", window=WINDOW)}
    assert results["activities"].error is None and results["activities"].normalise_errors == 0
    assert results["daily"].fetched == 14
    done = _count(runtime, activity, activity.c.source == "demo")
    assert done == results["activities"].fetched and 6 <= done <= 10
    assert _count(runtime, activity_unified) == done  # through unify, one row per session
    with runtime.engine.connect() as conn:
        rows = conn.execute(select(activity_unified).order_by(activity_unified.c.start_utc)).all()
    for row in rows:
        assert row.primary_source == "demo" and row.stream_key == row.group_id.split(":", 1)[1]
        assert row.avg_speed and row.avg_hr and row.distance_m and row.moving_s
        table = pq.read_table(runtime.parquet.stream_path(row.stream_key))
        assert {"t", "hr", "speed", "cadence", "alt", "dist", "lat", "lon"} <= set(
            table.column_names
        )
        assert table.num_rows == row.elapsed_s / demo.SAMPLE_S
    assert {r.sport for r in rows} == {"running", "cycling"}
    assert _count(runtime, activity_lap, activity_lap.c.source == "demo") > done * 5
    # Every night has sleep with stages and HRV; weight on some days.
    assert _count(runtime, sleep_session) == 14 and _count(runtime, sleep_stage) > 14 * 10
    with runtime.engine.connect() as conn:
        per_metric = dict(
            conn.execute(
                select(daily_unified.c.metric, func.count()).group_by(daily_unified.c.metric)
            ).all()
        )
    assert per_metric["sleep.duration"] == per_metric["hrv.night_avg"] == 14
    assert per_metric["sleep.score"] == 14 and per_metric["daily.steps"] == 14
    assert 2 <= per_metric["body.weight"] <= 9
    assert _count(runtime, body_measurement) == per_metric["body.weight"]

    # Again: nothing new, nothing doubled.
    again = {r.dataset: r for r in runtime.sync.sync("demo", window=WINDOW)}
    assert again["activities"].new_versions == 0 and again["daily"].new_versions == 0
    assert _count(runtime, activity, activity.c.source == "demo") == done
    assert _count(runtime, activity_unified) == done
    assert _count(runtime, sleep_session) == 14


def test_old_daily_items_without_sleep_still_map(runtime) -> None:
    """Items archived before the demo had nights hold steps and resting heart rate only."""
    stored = runtime.raw.put(
        source="demo",
        dataset="daily",
        item_key="2026-03-01",
        payload=json.dumps({"date": "2026-03-01", "steps": 5000, "resting_hr": 47}).encode(),
        content_type="application/json",
    )
    records = list(demo.map_daily(runtime.raw.load(stored.id)))
    assert [r.metric for r in records] == ["daily.steps", "hr.resting"]
