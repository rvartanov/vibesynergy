"""Worker loop resilience, sync timeouts and lane health."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.sync import lanes
from vibesynergy.sync.engine import SyncTimeout
from vibesynergy.sync.worker import Worker


def test_worker_survives_operational_error_from_claim(runtime, monkeypatch) -> None:
    q = runtime.jobs
    calls = {"n": 0}
    real_claim = q.claim

    def flaky_claim(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(q, "claim", flaky_claim)
    job_id = q.enqueue("ping", {})
    stop = threading.Event()
    worker = Worker(q, {"ping": lambda _job: None})
    thread = threading.Thread(target=worker.run, kwargs={"stop": stop, "poll_seconds": 0.05})
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if q.recent(status="done") and q.recent(status="done")[0]["id"] == job_id:
                break
            time.sleep(0.02)
        else:
            pytest.fail("job did not complete")
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_health_reports_dead_lane(runtime) -> None:
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    prior = dict(lanes._lanes)
    lanes._lanes.clear()
    try:
        lanes.register("synthetic", dead)
        body = TestClient(create_app(runtime)).get("/vibesynergy/health").json()
        assert body["ok"] is False
        assert body["lanes"]["synthetic"] is False
    finally:
        lanes._lanes.clear()
        lanes._lanes.update(prior)


def test_sync_job_times_out(runtime, monkeypatch) -> None:
    from vibesynergy.sync.engine import _check_sync_deadline

    entered = threading.Event()

    def slow_sync(source_id, datasets=None, window=None, *, deadline=None):
        entered.set()
        while True:
            _check_sync_deadline(deadline)
            time.sleep(0.01)

    settings = replace(runtime.settings, schedule={"sync_job_timeout_minutes": 1 / 60})
    runtime.sync._settings = settings
    monkeypatch.setattr(runtime.sync, "sync", slow_sync)
    runtime.jobs.enqueue("sync", {"source": "demo"})
    stop = threading.Event()
    worker = runtime.worker(lane="sync")
    thread = threading.Thread(target=worker.run, kwargs={"stop": stop, "poll_seconds": 0.05})
    thread.start()
    try:
        assert entered.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = runtime.jobs.recent()[0]
            if row["status"] != "running" and "timed out" in (row.get("last_error") or ""):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"job not failed on timeout: {runtime.jobs.recent()[0]}")
        assert runtime.jobs.counts().get("running", 0) == 0
    finally:
        stop.set()
        thread.join(timeout=10)


def test_sync_raises_when_deadline_is_already_past(runtime) -> None:
    with pytest.raises(SyncTimeout):
        runtime.sync.sync("demo", deadline=time.monotonic() - 0.01)


def test_sync_deadline_helper() -> None:
    from vibesynergy.sync.engine import _check_sync_deadline

    with pytest.raises(SyncTimeout):
        _check_sync_deadline(time.monotonic() - 1)


def test_health_says_how_long_the_oldest_job_has_run(runtime) -> None:
    client = TestClient(create_app(runtime))
    assert client.get("/vibesynergy/health").json()["oldest_running_s"] is None
    runtime.jobs.enqueue("sync", {})
    runtime.jobs.claim("worker")
    assert client.get("/vibesynergy/health").json()["oldest_running_s"] >= 0
