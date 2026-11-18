"""When coach runs are due, and how they reach the job queue.

The scheduler asks ``due`` on every tick (a cheap read); when a job is due it queues the
``coach_schedule`` job, which queues one ``coach_run`` job per due run. ``coach_run`` jobs run
in their own worker lane (``coach``), so a run of several minutes never holds up syncs or
webhooks. Times are local (``Europe/Amsterdam``) and set in Coach → Settings (default morning
07:00, evening 21:45, weekly review Sunday 19:44; ``settings.DEFAULT_SCHEDULE``). A run is still
started up to ``GRACE`` later (after a restart, say), never on the next day. Each job runs once
per local day (``coach_run`` keeps (job, date) unique). ``enqueue_test`` queues one run now for
the test button; its run key is its own, so the scheduled run of that day still follows.

Catch-up (task 16): when ``coach.run_missed`` says today's report is missing, the
window plus ``GRACE`` has passed and no run of that day exists at all (the service was down,
say), the report is queued once, late. A run that failed is not retried here; its failure
notice already went out.

Continuous advice (#90): between the fixed runs, new data makes a new version of the
current advice (``triggers.due``: the change, the wait, the daily limit). That is also how
the injury check and the check-in reaction start.

The insight run is not scheduled: ``on_agent_event`` is an internal subscriber of the agent
events (``query/agent_events.py``) and queues a ``coach_run`` for every ``activity.published``
event with an activity still missing its public insight, in the same transaction as the event.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.coach_runner import runner, triggers
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import multisport
from vibesynergy.store.schema import job as job_table

log = logging.getLogger(__name__)

JOB = "coach_run"
SCHEDULE_JOB = "coach_schedule"
LANE_KINDS = (JOB,)
GRACE = timedelta(hours=3)
MAX_ATTEMPTS = runner.MAX_ATTEMPTS


def _active(rt) -> runner_settings.RunnerSettings | None:
    cfg = runner_settings.load(rt)
    return cfg if cfg.enabled and cfg.api_key_set else None


def due_runs(rt, now: datetime | None = None) -> list[dict[str, Any]]:
    """The scheduled runs due now: inside their window and not run yet today."""
    now = now or utc_now()
    cfg = _active(rt)
    if cfg is None or runner.over_budget(rt, cfg, now):
        return []
    tz = runner.zone(rt)
    local = now.astimezone(tz)
    day = local.date()
    out = []
    for name, spec in cfg.schedule.items():
        if not spec["on"] or day.weekday() not in spec["days"]:
            continue
        if "monthday" in spec and day.day != spec["monthday"]:
            continue
        if name == "race" and not races_ahead(rt, day):
            continue
        if name == "block" and block_ending(rt, day) is None:
            continue
        at = time.fromisoformat(spec["time"])
        start = datetime.combine(day, at, tzinfo=tz)
        if not start <= local < start + GRACE:
            continue
        run_key = day.isoformat()
        if name == "thresholds":
            key = signal_key(rt, day)
            if key is None:
                continue
            run_key = key  # one run per set of signals, not one a week
        existing = runner.get_run(rt, name, run_key)
        if existing is not None and existing["status"] != "budget":
            continue
        out.append({"job": name, "run_key": run_key, "for_date": day.isoformat()})
    # New data since the current advice (#90): sleep, HRV, check-in, a note, pain or an
    # injury, a workout, the plan, a reply. The injury and check-in tasks run from here.
    if (update := triggers.due(rt, cfg, now)) is not None:
        out.append(update)
    for session_id, ids in tests_done(rt, cfg, day):
        out.append(
            {
                "job": "test",
                "run_key": session_id,
                "for_date": day.isoformat(),
                "payload": {"activity_ids": ids, "session_id": session_id},
            }
        )
    return out


_TEST_NAME = re.compile(r"test|\bftp\b|\bcss\b", re.IGNORECASE)  # "Looptest", "FTP-test"


def tests_done(rt, cfg, day: date) -> list[tuple[str, list[str]]]:
    """Task 10: today's planned test sessions (named test, FTP or CSS) that have an
    activity of the same sport today, and no evaluation yet. One run per test session."""
    from vibesynergy.query import plan_model
    from vibesynergy.query.plan import family
    from vibesynergy.store.schema import activity_unified

    if cfg.tasks.get("test", {}).get("on") is False:
        return []
    try:
        sessions = plan_model.view(rt.engine, day, day).get("sessions") or []
    except Exception:
        log.exception("reading today's plan failed")
        return []
    tests = [s for s in sessions if _TEST_NAME.search(s.get("name") or "")]
    if not tests:
        return []
    with rt.engine.connect() as conn:
        done = conn.execute(
            select(activity_unified.c.group_id, activity_unified.c.sport).where(
                activity_unified.c.local_date == day
            )
        ).all()
    out = []
    for session in tests:
        ids = [g for g, sport in done if family(sport) == family(session.get("sport"))]
        existing = runner.get_run(rt, "test", session["session_id"])
        if ids and (existing is None or existing["status"] == "budget"):
            out.append((session["session_id"], ids))
    return out


def due(rt, now: datetime | None = None) -> bool:
    """Asked on every scheduler tick; a failure here never stops the other syncs."""
    try:
        return bool(due_runs(rt, now))
    except Exception:
        log.exception("checking for due coach runs failed")
        return False


def dedupe_key(job: str, run_key: str) -> str:
    return f"{JOB}:{job}:{run_key}"


def enqueue_due(rt, now: datetime | None = None) -> list[int]:
    queued = []
    for item in due_runs(rt, now):
        job_id = rt.jobs.enqueue(
            JOB,
            {"trigger": "schedule", **item},
            dedupe_key=dedupe_key(item["job"], item["run_key"]),
            max_attempts=MAX_ATTEMPTS,
            priority=5,
        )
        if job_id:
            queued.append(job_id)
    return queued


class TestRunError(ValueError):
    """The test run was not queued; plain English for the page to translate."""


def enqueue_test(rt, job: str, now: datetime | None = None) -> int:
    """Queue one report run now (Coach → Settings → Test now). It costs like any run."""
    from vibesynergy.coach_runner import REPORT_JOBS

    if job not in REPORT_JOBS:
        raise TestRunError("Unknown report.")
    if _active(rt) is None:
        raise TestRunError("Switch Coach via API on first.")
    now = now or utc_now()
    day = now.astimezone(runner.zone(rt)).date()
    run_key = f"test-{now.strftime('%Y%m%dT%H%M%S')}"
    job_id = rt.jobs.enqueue(
        JOB,
        {"job": job, "run_key": run_key, "for_date": day.isoformat(), "trigger": "test"},
        dedupe_key=dedupe_key(job, run_key),
        max_attempts=1,
        priority=5,
    )
    if not job_id:
        raise TestRunError("A test of this report is already running.")
    return job_id


def enqueue_advice(
    rt,
    day: date,
    run_key: str,
    *,
    trigger: str,
    wait_for: list[int] | None = None,
    now: datetime | None = None,
) -> int | None:
    """Queue one daily advice run now (#89): the manual refresh on the dashboard, or a hard
    signal that came in after the day's advice. ``wait_for`` are sync jobs it waits for
    (at most ``SYNC_WAIT``), so the advice reads the fresh data. ``None`` when that run is
    already queued. The caller checks that the runner is on; a run while it is off ends
    as ``off`` and queues nothing else."""
    from vibesynergy.query import daily_advice

    now = now or utc_now()
    payload: dict[str, Any] = {
        "job": daily_advice.RERUN_JOB,
        "run_key": run_key,
        "for_date": day.isoformat(),
        "trigger": trigger,
    }
    if wait_for:
        payload["wait_for"] = [int(i) for i in wait_for]
        payload["requested_at"] = now.isoformat()
    return rt.jobs.enqueue(
        JOB,
        payload,
        dedupe_key=dedupe_key(daily_advice.RERUN_JOB, run_key),
        max_attempts=MAX_ATTEMPTS,
        priority=6,
    )


SYNC_WAIT = timedelta(minutes=10)  # a refresh waits this long at most for its syncs
SYNC_POLL = timedelta(seconds=10)


def _syncs_busy(rt, ids: list[int]) -> bool:
    if not ids:
        return False
    with rt.engine.connect() as conn:
        found = conn.execute(
            select(job_table.c.id).where(
                job_table.c.id.in_(ids), job_table.c.status.in_(("queued", "running"))
            )
        ).first()
    return found is not None


def handle(rt, job, *, client=None) -> runner.RunResult:
    """The ``coach_run`` job. A retryable failure raises, so the queue tries once more.
    A run that waits for syncs (the manual refresh) is deferred while they still run."""
    p = job.payload
    if p.get("wait_for"):
        requested = datetime.fromisoformat(p.get("requested_at") or utc_now().isoformat())
        if utc_now() - requested < SYNC_WAIT and _syncs_busy(rt, p["wait_for"]):
            from vibesynergy.sync.worker import JobError

            raise JobError("waiting for the sync", retry_at=utc_now() + SYNC_POLL)
    return runner.run(
        rt,
        p["job"],
        p["run_key"],
        date.fromisoformat(p["for_date"]),
        trigger=p.get("trigger", "schedule"),
        payload=p.get("payload"),
        client=client,
    )


def on_agent_event(rt, conn, event: dict[str, Any]) -> int | None:
    """Internal subscriber for agent events: queue the insight run for activity.published.

    Runs inside the transaction that stores the event, so the run is queued exactly when
    the event exists. Nothing here leaves the process.
    """
    from vibesynergy.query.agent_events import ACTIVITY_PUBLISHED, COACH_RUN_MISSED

    cfg = _active(rt)
    if cfg is None:
        return None
    if event.get("type") == COACH_RUN_MISSED:
        return _catch_up(rt, conn, cfg, event)
    if event.get("type") != ACTIVITY_PUBLISHED:
        return None
    if cfg.tasks.get("insight", {}).get("on") is False:
        return None
    activities = [
        a
        for a in (event.get("data") or {}).get("activities") or []
        if a.get("public_insight_missing", True)
    ]
    if not activities:
        return None
    key = dedupe_key("insight", event["id"])
    if conn.scalar(
        select(job_table.c.id).where(
            job_table.c.dedupe_key == key, job_table.c.status.in_(("queued", "running"))
        )
    ):
        return None
    now = utc_now()
    days = sorted(a.get("local_date") or "" for a in activities)
    payload = {
        "job": "insight",
        "run_key": event["id"],
        "for_date": days[-1] or now.astimezone(runner.zone(rt)).date().isoformat(),
        "trigger": "event",
        "payload": {
            "event_type": event["type"],
            "event_id": event["id"],
            # A multisport part brings its holder and the other parts.
            "activity_ids": multisport.relatives(rt.engine, [a["id"] for a in activities]),
        },
    }
    return conn.execute(
        job_table.insert()
        .values(
            kind=JOB,
            payload=payload,
            status="queued",
            priority=6,
            run_after=now,
            attempts=0,
            max_attempts=MAX_ATTEMPTS,
            dedupe_key=key,
            created_at=now,
        )
        .returning(job_table.c.id)
    ).scalar_one()


def _catch_up(rt, conn, cfg, event: dict[str, Any], now: datetime | None = None) -> int | None:
    """Queue today's missed report once (task 16), only when its window has passed and no
    run of that day exists. Never a report for an earlier day."""
    data = event.get("data") or {}
    name = data.get("task")
    spec = cfg.schedule.get(name)
    if spec is None or not spec["on"] or cfg.tasks.get("catch_up", {}).get("on") is False:
        return None
    now = now or utc_now()
    tz = runner.zone(rt)
    today = now.astimezone(tz).date()
    if data.get("date") != today.isoformat():
        return None
    start = datetime.combine(today, time.fromisoformat(spec["time"]), tzinfo=tz)
    if now.astimezone(tz) < start + GRACE or runner.get_run(rt, name, today.isoformat()):
        return None
    key = dedupe_key(name, today.isoformat())
    if conn.scalar(select(job_table.c.id).where(job_table.c.dedupe_key == key)):
        return None
    return conn.execute(
        job_table.insert()
        .values(
            kind=JOB,
            payload={
                "job": name,
                "run_key": today.isoformat(),
                "for_date": today.isoformat(),
                "trigger": "catch_up",
            },
            status="queued",
            priority=5,
            run_after=now,
            attempts=0,
            max_attempts=MAX_ATTEMPTS,
            dedupe_key=key,
            created_at=now,
        )
        .returning(job_table.c.id)
    ).scalar_one()


def races_ahead(rt, day: date) -> list[dict[str, Any]]:
    """Races exactly ``RACE_DAYS_BEFORE`` days after ``day`` (task 13): what a brief is for."""
    from vibesynergy.query import plan

    try:
        races = plan.calendar_races(rt.engine, day)
    except Exception:
        log.exception("reading the race calendar failed")
        return []
    return [r for r in races if r.get("days_away") in runner_settings.RACE_DAYS_BEFORE]


def block_ending(rt, day: date) -> dict[str, Any] | None:
    """The plan phase whose last week ends on ``day`` (task 12): the week with ``day`` has
    another phase than the week after it, or is the plan's last week. A phase of one week
    gets no review of its own (Cross and Herstel are one week each; one review
    is enough). None otherwise."""
    from vibesynergy.query import plan_model

    try:
        found = plan_model.view(rt.engine, day - timedelta(days=7), day + timedelta(days=7))
    except Exception:
        log.exception("reading the plan weeks failed")
        return None
    weeks = sorted(found.get("weeks") or [], key=lambda w: w["monday"])
    this = next((w for w in reversed(weeks) if w["monday"] <= day.isoformat()), None)
    if this is None or date.fromisoformat(this["monday"]) + timedelta(days=6) != day:
        return None
    after = next((w for w in weeks if w["monday"] > day.isoformat()), None)
    if after is not None and after.get("phase") == this.get("phase"):
        return None
    before = [w for w in weeks if w["monday"] < this["monday"]]
    if not before or before[-1].get("phase") != this.get("phase"):
        return None  # a one-week phase
    return {"phase": this.get("phase"), "next_phase": after.get("phase") if after else None}


def signal_key(rt, day: date) -> str | None:
    """Task 11: a key for today's threshold signals (None without any), so the same signals
    give one run and a new or changed one gives the next."""
    import hashlib
    import json

    from vibesynergy.query import thresholds

    try:
        found = thresholds.signals(rt.engine, day)
    except Exception:
        log.exception("reading the threshold signals failed")
        return None
    if not found:
        return None
    stable = [{k: s[k] for k in ("sport", "metric", "measured")} for s in found]
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:12]
    return f"signals-{digest}"
