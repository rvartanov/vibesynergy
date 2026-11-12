"""End to end with the demo source: fetch → raw → normalise → canonical rows → state."""

from dataclasses import replace
from datetime import date

from sqlalchemy import func, select

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import daily_metric


def _metric_rows(rt) -> int:
    with rt.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(daily_metric)).scalar()


def test_initial_then_incremental_sync(runtime) -> None:
    results = {r.dataset: r for r in runtime.sync.sync("demo")}
    daily = results["daily"]
    assert daily.error is None
    assert (daily.fetched, daily.new_versions, daily.records) == (14, 14, 28)
    assert results["profile"].window is None and results["profile"].fetched == 1
    assert _metric_rows(runtime) == 28

    state = runtime.sync.state("demo", "daily")
    today = today_in("Europe/Amsterdam")
    assert state["synced_until"] == today and state["last_error"] is None

    # Incremental: only the refetch window, and identical content creates no versions.
    again = runtime.sync.sync("demo", ["daily"])[0]
    assert again.window == Window(date.fromordinal(today.toordinal() - 1), today)
    assert (again.fetched, again.new_versions) == (2, 0)


def test_changed_data_supersedes_and_updates(runtime) -> None:
    window = Window(date(2026, 1, 1), date(2026, 1, 3))
    runtime.sync.sync("demo", ["daily"], window)

    settings = replace(runtime.settings, sources={"demo": {"enabled": True, "steps_offset": 1000}})
    rt2 = Runtime.open(settings)
    result = rt2.sync.sync("demo", ["daily"], window)[0]
    assert result.new_versions == 3
    assert _metric_rows(rt2) == 6  # replaced, not duplicated
    with rt2.engine.connect() as conn:
        steps = conn.execute(
            select(daily_metric.c.value).where(
                daily_metric.c.metric == "daily.steps", daily_metric.c.date == date(2026, 1, 1)
            )
        ).scalar()
    first = rt2.raw.load(next(rt2.raw.current_ids("demo", "daily"))).json()
    assert steps == first["steps"]
    rt2.close()


def test_backfill_does_not_move_synced_until_back(runtime) -> None:
    runtime.sync.sync("demo", ["daily"])
    runtime.sync.sync("demo", ["daily"], Window(date(2025, 1, 1), date(2025, 1, 2)))
    state = runtime.sync.state("demo", "daily")
    assert state["synced_from"] == date(2025, 1, 1)
    assert state["synced_until"] == today_in("Europe/Amsterdam")


def test_rebuild_from_raw(runtime) -> None:
    runtime.sync.sync("demo", ["daily"], Window(date(2026, 1, 1), date(2026, 1, 5)))
    with runtime.engine.begin() as conn:
        conn.execute(daily_metric.delete())
    assert runtime.normaliser.rebuild("demo") == (5, 0)
    assert _metric_rows(runtime) == 10


def test_fetch_error_is_recorded_not_raised(runtime, monkeypatch) -> None:
    source = runtime.registry.get("demo")

    def broken(dataset, window):
        raise ConnectionError("source down")
        yield

    monkeypatch.setattr(source, "fetch", broken)
    result = runtime.sync.sync("demo", ["daily"])[0]
    assert result.error == "ConnectionError: source down"
    state = runtime.sync.state("demo", "daily")
    assert state["last_error"] == result.error and state["synced_until"] is None


def test_worker_runs_sync_job(runtime) -> None:
    runtime.jobs.enqueue(
        "sync", {"source": "demo", "dataset": "daily", "window": "2026-02-01..2026-02-02"}
    )
    runtime.jobs.enqueue("mystery", {})
    worker = runtime.worker()
    assert worker.run_once() and worker.run_once() and not worker.run_once()
    statuses = {j["kind"]: j["status"] for j in runtime.jobs.recent()}
    assert statuses["sync"] == "done"
    assert statuses["mystery"] == "queued"  # failed once, backing off
    assert _metric_rows(runtime) == 4


def test_file_datasets_are_not_synced(runtime) -> None:
    source = runtime.registry.get("files", require_enabled=False)
    result = runtime.sync.sync_dataset(source, source.dataset("fit"))
    assert "vibesynergy import" in result.error
    assert runtime.sync.state("files", "fit") is None  # nothing recorded as attempted


def test_identical_content_retries_failed_mapping_and_job(runtime, monkeypatch) -> None:
    source = runtime.registry.get("demo")
    original = source.mappers

    def broken(raw):
        raise ValueError("synthetic mapping failure")

    monkeypatch.setattr(source, "mappers", lambda: {"daily": broken})
    runtime.jobs.enqueue("sync", {"source": "demo", "dataset": "daily", "window": "2026-02-01"})
    worker = runtime.worker()
    assert worker.run_once()
    job = runtime.jobs.recent()[0]
    assert job["status"] == "queued" and "failed to normalise" in job["last_error"]
    assert _metric_rows(runtime) == 0
    raw_ids = list(runtime.raw.all_ids("demo", "daily"))
    assert len(raw_ids) == 1 and runtime.raw.needs_normalising(raw_ids[0])
    # Collection succeeded; the separate mapping status and job report the failure.
    assert runtime.sync.state("demo", "daily")["last_error"] is None
    monkeypatch.setattr(source, "mappers", original)
    with runtime.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE job SET run_after = '2000-01-01T00:00:00.000000+00:00'")
    assert worker.run_once()
    assert runtime.jobs.recent()[0]["status"] == "done"
    assert _metric_rows(runtime) == 2
    assert list(runtime.raw.all_ids("demo", "daily")) == raw_ids
    assert not runtime.raw.needs_normalising(raw_ids[0])
    # Healthy unchanged content must remain cheap and must not run mapping again.
    monkeypatch.setattr(source, "mappers", lambda: {"daily": broken})
    result = runtime.sync.sync("demo", ["daily"], Window.parse("2026-02-01"))[0]
    assert (result.records, result.new_versions, result.normalise_errors) == (0, 0, 0)


def test_archived_immutable_item_retries_without_downloading(runtime) -> None:
    from vibesynergy.sources.base import FetchedItem
    from vibesynergy.store.raw import encode_payload
    from vibesynergy.sync.engine import DatasetResult

    payload, content_type = encode_payload({"date": "2026-02-01", "steps": 100, "resting_hr": 50})
    stored = runtime.raw.put(
        source="demo", dataset="daily", item_key="day", payload=payload, content_type=content_type
    )
    # Simulate an interruption after archiving but before mapping.
    assert runtime.raw.needs_normalising(stored.id)

    def no_download():
        raise AssertionError("an immutable item must use the archive")

    result = DatasetResult("demo", "daily", None)
    runtime.sync.ingest("demo", "daily", FetchedItem("day", no_download, immutable=True), result)
    assert (result.skipped, result.records, result.new_versions) == (1, 2, 0)
    assert not runtime.raw.needs_normalising(stored.id)


def test_mapping_failure_is_not_hidden_by_rate_limit() -> None:
    import pytest

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.sync.engine import DatasetResult
    from vibesynergy.sync.worker import JobError, _raise_failures

    with pytest.raises(JobError, match="failed to normalise") as failure:
        _raise_failures(
            [
                DatasetResult(
                    "demo", "daily", None, normalise_errors=1, error="limited", retry_at=utc_now()
                )
            ]
        )
    assert failure.value.retry_at is None


def test_a_window_further_on_does_not_hide_the_days_in_between(runtime) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from vibesynergy.store.schema import dataset_state

    today = today_in("Europe/Amsterdam")
    runtime.sync.sync("demo", ["daily"])
    behind = today - timedelta(days=20)
    with runtime.engine.begin() as conn:  # as after three weeks without a sync
        conn.execute(update(dataset_state).values(synced_until=behind))
    runtime.sync.sync("demo", ["daily"], Window(today, today))  # e.g. a webhook for today
    assert runtime.sync.state("demo", "daily")["synced_until"] == behind
    # A window that joins what was covered moves it on, as before.
    runtime.sync.sync("demo", ["daily"], Window(behind + timedelta(days=1), today))
    assert runtime.sync.state("demo", "daily")["synced_until"] == today
