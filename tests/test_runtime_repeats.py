"""Work the service no longer repeats at run time (#203): the publication settings, the start
page's training load and gear, the job queue's finished jobs and the audit-log indexes."""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from vibesynergy.core import config
from vibesynergy.core.config_edit import apply_changes
from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import (
    auto_names,
    dashboard,
    gear_cache,
    publication_auto,
    text_sync,
    thresholds,
    training,
    workouts,
)
from vibesynergy.store.schema import activity_unified, job
from vibesynergy.sync.jobs import PRUNE_DAYS
from vibesynergy.sync.scheduler import PRUNE_EVERY_S, Scheduler

# --- 1. publication settings ----------------------------------------------------------


def _settle(path) -> None:
    """Date the file back, as a file saved a while ago (past the settle window)."""
    old = time.time() - 60
    os.utime(path, (old, old))


def _counting_loads(monkeypatch) -> list[int]:
    calls = [0]
    real = config.load_settings

    def counted(path=None):
        calls[0] += 1
        return real(path)

    monkeypatch.setattr(config, "load_settings", counted)
    return calls


def test_scheduler_ticks_read_the_publication_settings_once(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[publication]\nauto_publish = true\ntext_sync_enabled = true\n")
    _settle(path)
    calls = _counting_loads(monkeypatch)
    rt = SimpleNamespace(settings=SimpleNamespace(config_path=path))
    for _ in range(10):  # ten scheduler ticks, each asking several modules
        assert publication_auto.enabled(rt) and text_sync.enabled(rt)
        auto_names.enabled(rt)
    assert calls[0] == 1
    # Each caller gets its own copy: a change to it is not seen by the next caller.
    config.current_publication(path)["auto_publish"] = False
    assert publication_auto.enabled(rt)


def test_a_saved_setting_counts_at_once(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[user]\ntimezone = "Europe/Amsterdam"\n\n[publication]\nauto_publish = true\n')
    _settle(path)
    rt = SimpleNamespace(settings=SimpleNamespace(config_path=path))
    assert publication_auto.enabled(rt)
    # A save from Settings (config_edit) replaces the file and drops the kept copy.
    apply_changes(path, {"publication": {"auto_publish": False}})
    _settle(path)
    assert not publication_auto.enabled(rt)
    # A hand edit on disk is seen too: same size, other content, settled time.
    path.write_text(path.read_text().replace("auto_publish = false", "auto_publish = true "))
    os.utime(path, (time.time() - 30, time.time() - 30))
    assert publication_auto.enabled(rt)


def test_an_edit_within_the_clock_resolution_is_not_missed(tmp_path) -> None:
    """Rewritten in place within moments, with the same size: still read again."""
    path = tmp_path / "config.toml"
    path.write_text('[publication]\nlanguage = "en"\n')
    assert config.current_publication(path)["language"] == "en"
    stamp = path.stat()
    path.write_text('[publication]\nlanguage = "nl"\n')
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # the very same mtime
    assert config.current_publication(path)["language"] == "nl"


# --- 2. start page: training load and gear ----------------------------------------------

DAY = date(2026, 6, 30)


def _put(rt, source: str, records, key: str) -> None:
    stored = rt.raw.put(
        source=source, dataset="test", item_key=key, payload=key.encode(), content_type="text/plain"
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _ride(source_id: str, day: date, hour: int, watts: float, **kw) -> Activity:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    return Activity(source_id, start, day, "cycling", moving_s=3600.0, norm_power=watts, **kw)


def _seed_load(runtime) -> None:
    """Rides over 200 days, some given as coaching, one coaching ride on top of an own ride
    (twins of one family), and a Strava copy of a Garmin ride."""
    rides = []
    for n in range(0, 200, 3):
        day = DAY - timedelta(days=n)
        rides.append(_ride(f"r{n}", day, 8, 150.0 + n % 60))
    rides.append(_ride("coach-1", DAY - timedelta(days=2), 18, 120.0))
    rides.append(_ride("coach-2", DAY - timedelta(days=20), 18, 140.0))
    # A coaching ride that overlaps an own ride of that day (dropped as a twin with it).
    rides.append(_ride("coach-twin", DAY - timedelta(days=9), 8, 110.0))
    _put(runtime, "garmin", rides, "rides")
    _put(runtime, "strava", [_ride("s-copy", DAY - timedelta(days=12), 8, 180.0)], "copy")
    unify.rebuild(runtime.engine)
    with runtime.engine.begin() as conn:
        groups = conn.execute(select(activity_unified.c.group_id, activity_unified.c.sources))
        coaching = [g for g, sources in groups if any("coach-" in s for s in sources or [])]
        conn.execute(
            activity_unified.update()
            .where(activity_unified.c.group_id.in_(coaching))
            .values(kind="coaching")
        )
    thresholds.set_value(
        runtime.engine, sport="bike", metric="ftp", value=200.0, date_set=DAY - timedelta(days=400)
    )


def _old_daily(runtime, window: Window, *, coaching: bool) -> dict:
    """The day totals as the old second pass gave them: ``activity_loads`` on its own."""
    daily: dict = defaultdict(float)
    for item in training.activity_loads(
        runtime.engine, window, runtime.settings.athlete, include_coaching=coaching
    ):
        daily[item["date"]] += item["load"]
    return daily


def test_one_pass_gives_the_same_load_with_and_without_coaching(runtime) -> None:
    _seed_load(runtime)
    window = Window(DAY - timedelta(days=60), DAY)
    warmup = Window(window.start - timedelta(days=126), DAY)
    # Loads stored first, so the old two passes and the one pass read the same stored values.
    training.activity_loads(runtime.engine, warmup, runtime.settings.athlete)
    out = training.training_load(runtime.engine, window, athlete=runtime.settings.athlete)
    every = _old_daily(runtime, warmup, coaching=True)
    own = _old_daily(runtime, warmup, coaching=False)
    expected = {str(d): round(v - own.get(d, 0), 1) for d, v in every.items() if v != own.get(d, 0)}
    expected = {d: v for d, v in expected.items() if d >= str(window.start)}
    assert out["coaching_daily_load"] == expected and expected  # coaching days present
    assert [r[1] for r in out["rows"]] == [round(every.get(d, 0.0), 1) for d in window.days()]


def _old_load(runtime, today: date) -> dict:
    """The start page's load block as computed before #203: the 180-day snapshot, plus a
    separate 28-day window with its own 126-day warm-up for the ACWR."""
    current = training.current_load(runtime, today)
    pmc = training.training_load(
        runtime.engine,
        Window(today - timedelta(days=27), today),
        athlete=runtime.settings.athlete,
        load="vibesynergy",
        raw=runtime.raw,
        parquet=runtime.parquet,
    )
    loads = [row[1] for row in pmc["rows"]]
    acute = statistics.fmean(loads[-7:]) if loads else 0.0
    chronic = statistics.fmean(loads) if loads else 0.0
    with runtime.engine.connect() as conn:
        last = conn.execute(
            select(func.max(activity_unified.c.local_date)).where(
                activity_unified.c.local_date <= today
            )
        ).scalar()
    return {
        **{**(pmc["current"] or {}), **current},
        "acwr": round(acute / chronic, 2) if chronic else None,
        "days_since_training": (today - last).days if last else None,
    }


@pytest.mark.parametrize("offset", [0, 5, 190])
def test_the_start_page_load_is_unchanged_and_computed_once(runtime, monkeypatch, offset) -> None:
    _seed_load(runtime)
    today = DAY - timedelta(days=offset)
    expected = _old_load(runtime, today)
    passes = [0]
    real = training._loads_with_kind

    def counted(*args, **kwargs):
        passes[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(training, "_loads_with_kind", counted)
    assert dashboard._load(runtime.engine, today, runtime) == expected
    assert passes[0] == 1  # one pass over the activities (four before)


def _seed_gear(runtime) -> None:
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(
            {"g1": {"name": "Shoes A", "distance": 300_000}, "b1": {"name": "Bike A"}}
        ).encode(),
        content_type="application/json",
    )
    runs = [
        Activity(
            f"s{n}",
            datetime(2026, 6, 1 + n, 7, tzinfo=UTC),
            date(2026, 6, 1 + n),
            "Run",
            moving_s=1800.0,
            distance_m=5000.0,
            extra={"gear_id": "g1"},
        )
        for n in range(5)
    ]
    _put(runtime, "strava", runs, "runs")
    unify.rebuild(runtime.engine)


def _uncached(runtime, monkeypatch, today: date) -> dict:
    with monkeypatch.context() as m:
        m.setattr(gear_cache, "cached", lambda _engine, _name, compute: compute())
        return workouts.gear(runtime.engine, runtime.raw, today)


def test_gear_is_kept_between_requests_and_stays_identical(runtime, monkeypatch) -> None:
    _seed_gear(runtime)
    reads = [0]
    real = workouts._read_group_gear

    def counted(engine):
        reads[0] += 1
        return real(engine)

    monkeypatch.setattr(workouts, "_read_group_gear", counted)
    first = workouts.gear(runtime.engine, runtime.raw, DAY)
    for _ in range(5):
        assert workouts.gear(runtime.engine, runtime.raw, DAY) == first
    assert reads[0] == 1
    assert first == _uncached(runtime, monkeypatch, DAY)


def test_gear_follows_a_gear_change_and_a_sync_at_once(runtime, monkeypatch) -> None:
    _seed_gear(runtime)
    before = workouts.gear(runtime.engine, runtime.raw, DAY)
    seen = {g["id"]: g["activities_seen"] for g in before["gear"]}
    assert seen["g1"] == 5 and seen["b1"] == 0
    # A gear change on one activity (the Gear register's write) counts at once.
    workouts.set_activity_gear(runtime.engine, "strava:s0", "strava:b1")
    after = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert {g["id"]: g["activities_seen"] for g in after["gear"]} == {"g1": 4, "b1": 1}
    assert after == _uncached(runtime, monkeypatch, DAY)
    # A sync that brings a new activity with that gear counts at once too.
    new = Activity(
        "s9",
        datetime(2026, 6, 20, 7, tzinfo=UTC),
        date(2026, 6, 20),
        "Run",
        moving_s=1800.0,
        distance_m=5000.0,
        extra={"gear_id": "g1"},
    )
    _put(runtime, "strava", [new], "new")
    unify.rebuild(runtime.engine)
    synced = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert {g["id"]: g["activities_seen"] for g in synced["gear"]} == {"g1": 5, "b1": 1}
    assert synced == _uncached(runtime, monkeypatch, DAY)


def test_kept_gear_expires_for_writes_from_another_process(runtime, monkeypatch) -> None:
    _seed_gear(runtime)
    workouts.gear(runtime.engine, runtime.raw, DAY)
    clock = [time.monotonic() + gear_cache.MAX_AGE_S + 1]
    monkeypatch.setattr(gear_cache.time, "monotonic", lambda: clock[0])
    reads = [0]
    real = workouts._read_group_gear
    monkeypatch.setattr(
        workouts, "_read_group_gear", lambda e: reads.__setitem__(0, reads[0] + 1) or real(e)
    )
    workouts.gear(runtime.engine, runtime.raw, DAY)
    assert reads[0] == 1


# --- 3. finished jobs pruned daily ------------------------------------------------------


def _finished(runtime, kind: str, days_ago: float, status: str = "done") -> None:
    at = utc_now() - timedelta(days=days_ago)
    with runtime.engine.begin() as conn:
        conn.execute(
            job.insert().values(
                kind=kind, payload={}, status=status, priority=5, run_after=at, attempts=1,
                max_attempts=3, created_at=at, started_at=at, finished_at=at,
            )
        )  # fmt: skip


def test_the_scheduler_prunes_finished_jobs_once_a_day(runtime) -> None:
    for days in (PRUNE_DAYS + 10, PRUNE_DAYS + 5, 2):
        _finished(runtime, "alerts", days)
    _finished(runtime, "alerts", PRUNE_DAYS + 20, status="dead")  # failed: kept for a look
    _finished(runtime, "history", PRUNE_DAYS + 20)  # imports resume from it: kept
    _finished(runtime, "dropfolder", PRUNE_DAYS + 20)  # newest of its kind: kept
    now = [1000.0]
    scheduler = Scheduler(runtime.settings, runtime.registry, runtime.jobs, clock=lambda: now[0])

    def finished() -> list[tuple[str, str]]:
        with runtime.engine.connect() as conn:
            rows = conn.execute(
                select(job.c.kind, job.c.status).where(job.c.finished_at.is_not(None))
            )
            return sorted((r.kind, r.status) for r in rows)

    scheduler.run_once()
    kept = [("alerts", "dead"), ("alerts", "done"), ("dropfolder", "done"), ("history", "done")]
    assert finished() == kept
    # The same retention as `vibesynergy jobs prune`: nothing more for the CLI to remove.
    assert runtime.jobs.prune(older_than=timedelta(days=PRUNE_DAYS), statuses=("done",)) == {}
    # Not again until a day later.
    _finished(runtime, "alerts", PRUNE_DAYS + 3)
    _finished(runtime, "alerts", 1)  # the newest alerts job from now on
    now[0] += PRUNE_EVERY_S - 60
    scheduler.run_once()
    assert len(finished()) == 6
    now[0] += 120
    scheduler.run_once()
    assert finished() == sorted([*kept, ("alerts", "done")])


def test_a_failing_prune_does_not_stop_the_syncs(runtime, monkeypatch) -> None:
    def broken(**_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runtime.jobs, "prune", broken)
    scheduler = Scheduler(runtime.settings, runtime.registry, runtime.jobs, clock=lambda: 0.0)
    assert isinstance(scheduler.run_once(), list)


# --- 4. audit-log indexes ---------------------------------------------------------------


def test_audit_log_lookups_use_the_new_indexes(runtime) -> None:
    with runtime.engine.connect() as conn:
        names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'")
            )
        }
        assert {"ix_audit_log_action_target", "ix_audit_log_at"} <= names
        by_target = " ".join(
            str(r[3])
            for r in conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT id FROM audit_log "
                    "WHERE action = 'x.check' AND target = 'x:2026-01-01' LIMIT 1"
                )
            )
        )
        by_time = " ".join(
            str(r[3])
            for r in conn.execute(
                text("EXPLAIN QUERY PLAN DELETE FROM audit_log WHERE at < '2026-01-01'")
            )
        )
    assert "ix_audit_log_action_target" in by_target
    assert "ix_audit_log_at" in by_time
