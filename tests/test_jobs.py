from datetime import timedelta

from vibesynergy.core.timeutil import utc_now
from vibesynergy.sync.jobs import backoff


def test_enqueue_claim_complete(runtime) -> None:
    q = runtime.jobs
    low = q.enqueue("sync", {"source": "demo"})
    high = q.enqueue("sync", {"source": "demo", "dataset": "daily"}, priority=5)
    assert q.claim("w").id == high
    assert q.claim("w").id == low
    assert q.claim("w") is None
    q.complete(high)
    assert q.counts() == {"done": 1, "running": 1}


def test_dedupe_only_while_active(runtime) -> None:
    q = runtime.jobs
    first = q.enqueue("sync", {}, dedupe_key="k")
    assert q.enqueue("sync", {}, dedupe_key="k") is None
    q.complete(q.claim("w").id)
    assert q.enqueue("sync", {}, dedupe_key="k") not in (None, first)


def test_fail_backs_off_then_dies(runtime) -> None:
    q = runtime.jobs
    job_id = q.enqueue("sync", {}, max_attempts=2)
    q.fail(q.claim("w").id, "boom")
    assert q.claim("w") is None  # backing off
    assert q.recent()[0]["status"] == "queued"

    with runtime.engine.begin() as conn:  # skip the wait
        conn.exec_driver_sql("UPDATE job SET run_after = '2000-01-01T00:00:00.000000+00:00'")
    job = q.claim("w")
    assert job.id == job_id and job.attempts == 2
    q.fail(job.id, "boom again")
    assert q.recent()[0]["status"] == "dead"


def test_backoff_is_capped() -> None:
    assert backoff(1) == timedelta(seconds=30)
    assert backoff(3) == timedelta(minutes=2)
    assert backoff(20) == timedelta(hours=1)


def test_defer_keeps_the_attempt(runtime) -> None:
    q = runtime.jobs
    job_id = q.enqueue("sync", {}, max_attempts=1)
    q.defer(q.claim("w").id, utc_now() + timedelta(hours=1), "rate limited")
    assert q.claim("w") is None  # waiting for the reset
    (job,) = q.recent()
    assert job["id"] == job_id and job["status"] == "queued" and job["attempts"] == 0


def test_recovery_requires_dead_owner_and_does_not_consume_attempt(runtime) -> None:
    q = runtime.jobs
    for owner in ("dead", "alive", "unknown"):
        q.enqueue("sync", {}, dedupe_key=owner)
        q.claim(owner)
    # Even very old jobs must not be stolen from a live or unidentifiable owner.
    with runtime.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET started_at = '2000-01-01T00:00:00.000000+00:00' "
            "WHERE locked_by != 'dead'"
        )

    def probe(owner):
        return {"dead": False, "alive": True, "unknown": None}[owner]

    assert q.requeue_abandoned(probe) == 1
    assert q.requeue_abandoned(probe) == 0
    recovered = q.claim("replacement")
    assert recovered.attempts == 1
    assert q.claim("replacement") is None
    assert q.enqueue("sync", {}, dedupe_key="dead") is None


def test_recovery_does_not_overwrite_concurrent_completion(runtime) -> None:
    q = runtime.jobs
    jid = q.enqueue("sync", {})
    q.claim("dead")

    def probe(owner):
        q.complete(jid)
        return False

    assert q.requeue_abandoned(probe) == 0
    assert q.counts() == {"done": 1}


def test_worker_recovers_owner_that_dies_after_start(runtime, monkeypatch) -> None:
    from vibesynergy.sync.worker import Worker

    q = runtime.jobs
    jid = q.enqueue("test", {})
    q.claim("old")
    alive = [True]
    monkeypatch.setattr("vibesynergy.sync.worker._owner_alive", lambda owner: alive[0])
    handled = []
    worker = Worker(q, {"test": lambda job: handled.append(job.id)})
    assert not worker.run_once()
    alive[0] = False
    assert worker.run_once()
    assert handled == [jid] and q.counts() == {"done": 1}


def test_local_owner_probe_is_conservative(monkeypatch) -> None:
    from vibesynergy.sync.worker import _owner_alive

    monkeypatch.setattr("socket.gethostname", lambda: "local")
    calls = []
    monkeypatch.setattr("os.kill", lambda pid, signal: calls.append((pid, signal)))
    for owner in (None, "foreign:123", "local:bad", "local:0", "local:-1"):
        assert _owner_alive(owner) is None
    assert calls == []
    assert _owner_alive("local:123") is True
    assert calls == [(123, 0)]
    for error, expected in ((ProcessLookupError, False), (PermissionError, True), (OSError, None)):

        def fail(pid, signal, exception=error):
            raise exception()

        monkeypatch.setattr("os.kill", fail)
        assert _owner_alive("local:123") is expected


def test_prune_old_finished_jobs_keeps_newest_per_kind_and_history(runtime) -> None:
    # done jobs pile up; prune counts first and keeps what status and imports read.
    from vibesynergy.store.schema import job

    q = runtime.jobs
    ids = [q.enqueue("sync", {"n": n}) for n in range(3)]
    keep_history = q.enqueue("history", {})
    for _ in range(4):
        q.complete(q.claim("w").id)
    with runtime.engine.begin() as conn:
        old = utc_now() - timedelta(days=40)
        conn.execute(job.update().values(finished_at=old))
    found = q.prune(older_than=timedelta(days=30), statuses=("done",))
    assert found == {"sync": 2}
    assert q.counts() == {"done": 4}  # only counted
    assert q.prune(older_than=timedelta(days=30), statuses=("done",), apply=True) == {"sync": 2}
    left = {r["id"] for r in q.recent(limit=10)}
    assert left == {ids[2], keep_history}
    assert q.prune(older_than=timedelta(days=60), statuses=("done",)) == {}
