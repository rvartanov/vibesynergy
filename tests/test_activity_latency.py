"""Passive timing uses source workout times and keeps first observations."""

from datetime import UTC, datetime, timedelta

from test_evidence import activity, put

from vibesynergy.core.timeutil import Window
from vibesynergy.query import latency, views
from vibesynergy.sources.base import FetchedItem
from vibesynergy.store.schema import activity_latency
from vibesynergy.sync.engine import DatasetResult


def test_first_delivery_stages_and_dashboard_visibility(runtime, monkeypatch):
    start = datetime(2026, 9, 20, 10, tzinfo=UTC)
    finish = start + timedelta(minutes=10)
    notice = finish + timedelta(minutes=2)
    fetch = notice + timedelta(seconds=20)
    processed = fetch + timedelta(seconds=4)
    visible = processed + timedelta(minutes=1)
    put(runtime, "garmin", activity())
    latency.stamp(runtime.engine, "garmin", "1", received_at=notice)
    latency.fetched(runtime.engine, "garmin", "1", fetch)
    monkeypatch.setattr(latency, "utc_now", lambda: processed)
    latency.processed(runtime.engine)
    rows = views.activities(runtime.engine, Window(start.date(), start.date()))["activities"]
    assert len(rows) == 1
    assert latency.recent(runtime.engine)[0]["dashboard_at"] is None
    monkeypatch.setattr(latency, "utc_now", lambda: visible)
    latency.dashboard_visible(runtime.engine, rows)
    latency.dashboard_visible(runtime.engine, rows)
    latency.stamp(runtime.engine, "garmin", "1", received_at=visible, fetched_at=visible)
    item = latency.recent(runtime.engine)[0]
    assert item["finished_at"] == finish.isoformat()
    assert item["received_at"] == notice.isoformat()
    assert item["fetched_at"] == fetch.isoformat()
    assert item["processed_at"] == processed.isoformat()
    assert item["dashboard_at"] == visible.isoformat()
    assert item["delays_s"]["total_s"] == 204
    assert item["delays_s"]["vibesynergy_s"] == 24
    assert item["assessed_at"] is None


def test_notification_can_precede_canonical_activity(runtime):
    received = datetime(2026, 9, 20, 10, 12, tzinfo=UTC)
    latency.stamp(runtime.engine, "strava", "new", received_at=received)
    pending = latency.recent(runtime.engine)[0]
    assert pending["finished_at"] is None
    assert pending["received_at"] == received.isoformat()
    assert pending["delays_s"]["total_s"] is None
    with runtime.engine.connect() as conn:
        row = conn.execute(activity_latency.select()).one()
    assert row.received_at == received


def test_strava_ingest_records_source_finish_and_processing(runtime, monkeypatch):
    from vibesynergy.sync import engine as sync_engine

    at = datetime(2026, 9, 20, 10, 14, tzinfo=UTC)
    monkeypatch.setattr(latency, "utc_now", lambda: at)
    monkeypatch.setattr(sync_engine, "utc_now", lambda: at)
    result = DatasetResult("strava", "activities", None)
    item = FetchedItem(
        "42",
        {
            "summary": {
                "id": 42,
                "start_date": "2026-09-20T10:00:00Z",
                "start_date_local": "2026-09-20T12:00:00Z",
                "elapsed_time": 600,
                "moving_time": 590,
                "sport_type": "Run",
            }
        },
    )
    runtime.sync.ingest("strava", "activities", item, result)
    runtime.sync.changed([result])
    row = latency.recent(runtime.engine)[0]
    assert row["source_id"] == "42"
    assert row["finished_at"] == "2026-09-20T10:10:00+00:00"
    assert row["fetched_at"] == at.isoformat()
    assert row["processed_at"] == at.isoformat()
    assert row["dashboard_at"] is None
