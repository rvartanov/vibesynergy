"""The same durable queue can isolate long I/O without duplicating jobs or schedules."""

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from vibesynergy.store.schema import job


def test_archive_lane_runs_while_source_sync_is_blocked(runtime, monkeypatch):
    from vibesynergy import history

    monkeypatch.setattr(history, "import_archive", lambda *args, **kwargs: None)
    entered, release = threading.Event(), threading.Event()

    def sync(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return []

    monkeypatch.setattr(runtime.sync, "sync", sync)
    runtime.jobs.enqueue("sync", {"source": "demo"}, priority=20)
    sync_worker = runtime.worker(lane="sync")
    thread = threading.Thread(target=sync_worker.run_once)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        runtime.jobs.enqueue(history.ARCHIVE_JOB, {"fill": 1}, priority=10)
        assert runtime.worker(lane="archive").run_once()
        assert not release.is_set()
        with runtime.engine.connect() as conn:
            assert conn.execute(
                select(job.c.kind).where(job.c.status == "running")
            ).scalars().all() == ["sync"]
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_webhook_lane_fetches_while_a_source_sync_is_blocked(runtime, monkeypatch):
    from vibesynergy.sync.engine import DatasetResult

    entered, release = threading.Event(), threading.Event()

    def sync(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return []

    fetched = []

    def fetch_entity(source_id, dataset, key):
        fetched.append((source_id, dataset, key))
        return DatasetResult(source_id, dataset, None)

    monkeypatch.setattr(runtime.sync, "sync", sync)
    monkeypatch.setattr(runtime.sync, "fetch_entity", fetch_entity)
    runtime.jobs.enqueue("sync", {"source": "demo"}, priority=0)
    thread = threading.Thread(target=runtime.worker(lane="sync").run_once)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        runtime.jobs.enqueue(
            "fetch_one",
            {"source": "strava", "dataset": "activities", "key": "42"},
            priority=10,
        )
        assert runtime.worker(lane="webhook").run_once()
        assert fetched == [("strava", "activities", "42")]
        assert not runtime.worker(lane="webhook").run_once()
        with runtime.engine.connect() as conn:
            assert conn.execute(
                select(job.c.kind).where(job.c.status == "running")
            ).scalars().all() == ["sync"]
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_sync_lane_does_not_claim_archive_jobs(runtime, monkeypatch):
    from vibesynergy import history

    monkeypatch.setattr(history, "import_archive", lambda *args, **kwargs: None)
    runtime.jobs.enqueue(history.ARCHIVE_JOB, {"fill": 1}, priority=0)
    assert not runtime.worker(lane="sync").run_once()
    assert runtime.worker(lane="archive").run_once()
    assert not runtime.worker(lane="sync").run_once()


def test_unknown_lane_is_rejected(runtime):
    import pytest

    with pytest.raises(ValueError, match="Unknown worker lane"):
        runtime.worker(lane="assessment")


def test_concurrent_claims_preserve_lanes_and_claim_each_job_once(runtime):
    for i in range(20):
        runtime.jobs.enqueue("a" if i % 2 else "b", {"i": i}, priority=i)

    def claim(kind):
        found = []
        while current := runtime.jobs.claim("synthetic-owner", kinds=(kind,)):
            found.append((current.id, current.kind))
        return found

    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(claim, ["a", "b", "a", "b"]))
    found = [item for group in groups for item in group]
    assert len(found) == len({ident for ident, _ in found}) == 20
    for wanted, group in zip(["a", "b", "a", "b"], groups, strict=True):
        assert all(kind == wanted for _, kind in group)
    assert runtime.jobs.claim("synthetic", kinds=()) is None
