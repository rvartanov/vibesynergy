"""Continuous advice (#90): when the day's advice is made again, and for which day.

There is one current advice per target day. The fixed advice run (the ``morning`` task,
07:00 by default, Coach → Settings) makes today's; the evening run makes tomorrow's. In
between, new data makes a new version of the current advice:

- sleep or HRV comes in or is updated (``sleep``, ``hrv``);
- the night's sleep check changes its mark (``sleep_check``, #100: a night found badly
  recorded or incomplete, or the athlete's "it's right" / "yes, incomplete");
- the check-in is saved (``checkin``);
- a note (illness is a note tagged sick), pain or an injury comes in (``note``,
  ``injury``): at once, without waiting;
- a workout is done or changed (``activity``);
- the plan changes for the target day or the day after it (``plan``);
- the athlete replies to a report (``reply``).

How a change is found: every daily-advice run stores a fingerprint of its inputs when it
starts (``coach_run.harness.inputs``, one short hash per kind). On every scheduler tick
``due`` compares the inputs now with the newest run for the target day. A kind whose hash
differs is a change. Comparing values, not ``updated_at``, means a sync that rewrites the
same night or a rebuild of the activity table starts nothing. The data can come from any
path (a sync, the portal, the MCP, an import): no write path needs a hook.

Wait: a change waits until nothing has changed for ``DEBOUNCE`` (so three syncs in a row
give one run), at most ``MAX_WAIT`` after the first change. A note or an injury does not
wait. Before the day's fixed advice run, changes for today wait for it (it reads them),
except a note or an injury.

Limit: at most ``max_runs`` automatic runs per local day (Coach → Settings, default
``DEFAULT_MAX_RUNS``; 0 switches the updates off). Scheduled runs and "Update advice" do
not count. When the limit is reached and new data is waiting, the fixed rules give the
advice with the reason ``limit`` (``daily_advice.REASONS``).

Which task runs: for tomorrow (after the evening run) the ``evening`` task; for today the
``injury`` task when pain or an injury came in, the ``checkin`` task when the check-in did,
else the ``morning`` task, each with its own model from Coach → Settings. A task switched
off falls back to the ``morning`` task.

The run key is ``event-<target day>-<hash of the inputs>``: one run per state of the data,
never twice for the same state.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from vibesynergy.core.timeutil import utc_now

log = logging.getLogger(__name__)

KINDS = (
    "sleep",
    "hrv",
    "sleep_check",
    "checkin",
    "note",
    "injury",
    "activity",
    "plan",
    "reply",
)
IMMEDIATE = frozenset({"note", "injury"})
DEBOUNCE = timedelta(minutes=5)
MAX_WAIT = timedelta(minutes=30)
DEFAULT_MAX_RUNS = 4
MAX_RUNS_LIMIT = 24
# Runs that count against the daily limit: these triggers and #89's new-signal re-run.
AUTO_TRIGGERS = ("event", "signal")
TRIGGER = "event"
STATE_DOC = "advice_triggers"
COST_DAYS = 30


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _round(value: Any, step: float) -> float | None:
    return None if value is None else round(float(value) / step) * step


# --- the inputs -------------------------------------------------------------------------------


def fingerprint(rt, target: date) -> dict[str, str]:
    """One short hash per kind of input for the advice of ``target``. Values only, so a
    rewrite of the same data gives the same hash."""
    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import (
        activity_unified,
        checkin,
        coach_reply,
        daily_unified,
        injury,
        injury_log,
        note,
        plan_session,
        sleep_session,
    )

    before = target - timedelta(days=1)
    after = target + timedelta(days=1)
    plan_id = plan_model.current_plan_id(rt.engine)
    with rt.engine.connect() as conn:
        metrics = conn.execute(
            select(daily_unified.c.metric, daily_unified.c.value)
            .where(
                daily_unified.c.date == target,
                or_(daily_unified.c.metric.like("sleep.%"), daily_unified.c.metric.like("hrv.%")),
            )
            .order_by(daily_unified.c.metric)
        ).all()
        nights = conn.execute(
            select(sleep_session.c.source, sleep_session.c.duration_s, sleep_session.c.score).where(
                sleep_session.c.local_date == target
            )
        ).all()
        day_checkin = (
            conn.execute(select(checkin).where(checkin.c.date == target)).mappings().first()
        )
        notes = conn.execute(
            select(note.c.id, note.c.updated_at)
            .where(note.c.start_date <= target, note.c.end_date >= before)
            .order_by(note.c.id)
        ).all()
        injuries = conn.execute(
            select(injury.c.id, injury.c.status, injury.c.updated_at)
            .where(or_(injury.c.status != "resolved", injury.c.resolved_on >= before))
            .order_by(injury.c.id)
        ).all()
        pains = conn.execute(
            select(
                injury_log.c.injury_id,
                injury_log.c.date,
                injury_log.c.pain,
                injury_log.c.timing,
                injury_log.c.affects_movement,
            )
            .where(injury_log.c.date.between(before, target))
            .order_by(injury_log.c.injury_id, injury_log.c.date)
        ).all()
        activities = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.sport,
                activity_unified.c.elapsed_s,
                activity_unified.c.distance_m,
                activity_unified.c.purpose,
                activity_unified.c.kind,
                activity_unified.c.training_load,
            )
            .where(activity_unified.c.local_date.between(before, target))
            .order_by(activity_unified.c.group_id)
        ).all()
        sessions = (
            conn.execute(
                select(
                    plan_session.c.id,
                    plan_session.c.date,
                    plan_session.c.name,
                    plan_session.c.sport,
                    plan_session.c.duration_s,
                    plan_session.c.distance_m,
                    plan_session.c.status,
                    plan_session.c.version,
                )
                .where(
                    plan_session.c.plan_id == plan_id,
                    plan_session.c.date.between(target, after),
                )
                .order_by(plan_session.c.id)
            ).all()
            if plan_id
            else []
        )
        replies = conn.execute(
            select(func.max(coach_reply.c.id), func.max(coach_reply.c.updated_at))
        ).one()
    sleep = [(m, _round(v, 0.1)) for m, v in metrics if m.startswith("sleep.")]
    sleep += [(s, _round(d, 60), _round(q, 1)) for s, d, q in nights]
    checked = (
        {k: v for k, v in day_checkin.items() if k not in ("updated_at", "subject_id")}
        if day_checkin
        else None
    )
    return {
        "sleep": _digest(sorted(sleep, key=str)),
        "hrv": _digest([(m, _round(v, 0.1)) for m, v in metrics if m.startswith("hrv.")]),
        "sleep_check": _digest(_sleep_mark(rt, target)),
        "checkin": _digest(checked),
        "note": _digest([tuple(r) for r in notes]),
        "injury": _digest([[tuple(r) for r in injuries], [tuple(r) for r in pains]]),
        "activity": _digest(
            [
                (g, s, _round(e, 60), _round(d, 100), p, k, _round(load, 1))
                for g, s, e, d, p, k, load in activities
            ]
        ),
        "plan": _digest([tuple(r) for r in sessions]),
        "reply": _digest(tuple(replies)),
    }


def _sleep_mark(rt, target: date) -> Any:
    """The night's mark as far as the advice is concerned: its state and what does not
    count (``query.sleep_quality``), not the time of the check."""
    from vibesynergy.query import sleep_quality

    try:
        found = sleep_quality.night(
            rt.engine, getattr(rt, "raw", None), target, rt.settings.timezone
        )
    except Exception:
        log.debug("sleep check for the advice inputs failed", exc_info=True)
        return "unavailable"
    if not found:
        return None
    return [found["state"], sorted(found.get("not_counted") or []), found.get("override")]


# --- the target day, the newest run, the limit -------------------------------------------------


def target_for(rt, today: date) -> date:
    """The day the current advice is for: tomorrow once the evening run has run (it makes
    tomorrow's advice), else today."""
    from vibesynergy.store.schema import coach_run

    with rt.engine.connect() as conn:
        evening = conn.scalar(
            select(coach_run.c.id)
            .where(coach_run.c.job == "evening", coach_run.c.for_date == today)
            .limit(1)
        )
    return today + timedelta(days=1) if evening is not None else today


def baseline(rt, target: date) -> dict[str, Any] | None:
    """The newest daily-advice run for ``target`` that stored its inputs."""
    from vibesynergy.query import daily_advice
    from vibesynergy.store.schema import coach_run

    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(coach_run)
            .where(
                coach_run.c.job.in_(daily_advice.ADVICE_JOBS),
                coach_run.c.for_date.in_((target, target - timedelta(days=1))),
            )
            .order_by(coach_run.c.started_at.desc(), coach_run.c.id.desc())
        ).mappings()
        for row in rows:
            if daily_advice.target_day(row["job"], row["for_date"]) != target:
                continue
            inputs = (row["harness"] or {}).get("inputs")
            if isinstance(inputs, dict):
                return {"id": row["id"], "inputs": inputs, "started_at": row["started_at"]}
    return None


def day_start(rt, local_day: date) -> datetime:
    from vibesynergy.coach_runner import runner

    return datetime.combine(local_day, time(0, 0), tzinfo=runner.zone(rt))


def runs_today(rt, now: datetime | None = None) -> int:
    """Automatic advice runs started today (local): what counts against the limit."""
    from vibesynergy.coach_runner import runner
    from vibesynergy.query import daily_advice
    from vibesynergy.store.schema import coach_run

    now = now or utc_now()
    start = day_start(rt, now.astimezone(runner.zone(rt)).date())
    with rt.engine.connect() as conn:
        return int(
            conn.scalar(
                select(func.count()).where(
                    coach_run.c.trigger.in_(AUTO_TRIGGERS),
                    # Only advice runs: an activity insight (also started by an event) is no
                    # advice update and does not use up the day's updates (19-11-2026).
                    coach_run.c.job.in_(daily_advice.ADVICE_JOBS),
                    coach_run.c.started_at >= start,
                )
            )
            or 0
        )


def max_runs(cfg) -> int:
    return int((getattr(cfg, "advice", None) or {}).get("max_runs", DEFAULT_MAX_RUNS))


def limit_reached(rt, cfg, now: datetime | None = None) -> bool:
    return runs_today(rt, now) >= max_runs(cfg)


def cost_per_run(rt, now: datetime | None = None) -> float | None:
    """The average cost of a daily-advice run over the last ``COST_DAYS`` days, for the
    settings page ("about $0.70 per update"). ``None`` before the first run."""
    from vibesynergy.query import daily_advice
    from vibesynergy.store.schema import coach_run

    now = now or utc_now()
    with rt.engine.connect() as conn:
        found = conn.scalar(
            select(func.avg(coach_run.c.cost_usd)).where(
                coach_run.c.job.in_(daily_advice.ADVICE_JOBS),
                coach_run.c.turns > 0,
                coach_run.c.started_at >= now - timedelta(days=COST_DAYS),
            )
        )
    return round(float(found), 4) if found is not None else None


def _held(rt, cfg, target: date, today: date, local: datetime) -> bool:
    """Changes for today wait for today's fixed advice run, which reads them."""
    from vibesynergy.coach_runner import runner, schedule

    if target != today:
        return False
    spec = cfg.schedule.get("morning") or {}
    if not spec.get("on") or today.weekday() not in spec.get("days", []):
        return False
    if runner.get_run(rt, "morning", today.isoformat()) is not None:
        return False
    start = datetime.combine(today, time.fromisoformat(spec["time"]), tzinfo=local.tzinfo)
    return local < start + schedule.GRACE


def job_for(cfg, target: date, today: date, changes: list[str]) -> str:
    """The task that makes the new version (see the module docstring)."""

    def on(task: str) -> bool:
        return (cfg.tasks.get(task) or {}).get("on") is not False

    if target > today:
        return "evening"
    if "injury" in changes and on("injury"):
        return "injury"
    if "checkin" in changes and on("checkin"):
        return "checkin"
    return "morning"


# --- the check ----------------------------------------------------------------------------------


def check(rt, cfg, now: datetime | None = None, *, remember: bool = False) -> dict[str, Any]:
    """Where the current advice stands against the data now.

    Returns ``{target, today, changes, held, limit, off, busy, seen, due, item}``. ``due`` is true
    when a new version should be made now; ``item`` is then the run for the job queue.
    ``remember`` keeps the debounce state (the scheduler); a read leaves it alone."""
    from vibesynergy.coach_runner import runner
    from vibesynergy.coach_runner import settings as runner_settings
    from vibesynergy.query import daily_advice

    now = now or utc_now()
    local = now.astimezone(runner.zone(rt))
    today = local.date()
    target = target_for(rt, today)
    out: dict[str, Any] = {
        "target": target,
        "today": today,
        "changes": [],
        "held": False,
        "limit": False,
        "off": max_runs(cfg) <= 0,
        "busy": False,
        "seen": False,
        "due": False,
        "item": None,
    }
    base = baseline(rt, target)
    if base is None:
        return out  # nothing to compare with yet; the next advice run stores its inputs
    current = fingerprint(rt, target)
    changes = [k for k in KINDS if k in base["inputs"] and base["inputs"][k] != current.get(k)]
    out["changes"] = changes
    store = runner_settings.store(rt)
    doc = dict(store.get(STATE_DOC) or {})
    key = target.isoformat()
    if not changes:
        if remember and key in doc:
            doc.pop(key)
            store.set(STATE_DOC, doc)
        return out
    digest = _digest(current)
    mine = doc.get(key)
    if not isinstance(mine, dict) or mine.get("digest") != digest:
        same_base = isinstance(mine, dict) and mine.get("base") == base["id"]
        first = mine["first"] if same_base else now.isoformat()
        mine = {"digest": digest, "base": base["id"], "first": first, "last": now.isoformat()}
        if remember:
            doc = {k: v for k, v in doc.items() if k >= (today - timedelta(days=1)).isoformat()}
            doc[key] = mine
            store.set(STATE_DOC, doc)
    urgent = bool(IMMEDIATE & set(changes))
    out["held"] = _held(rt, cfg, target, today, local) and not urgent
    out["limit"] = not out["off"] and limit_reached(rt, cfg, now)
    out["busy"] = daily_advice._pending(rt, target)
    if out["held"] or out["off"] or out["limit"] or out["busy"]:
        return out
    quiet = now - datetime.fromisoformat(mine["last"]) >= DEBOUNCE
    waited = now - datetime.fromisoformat(mine["first"]) >= MAX_WAIT
    if not (urgent or quiet or waited):
        return out
    job = job_for(cfg, target, today, changes)
    run_key = f"event-{key}-{digest}"
    if runner.get_run(rt, job, run_key) is not None:
        out["seen"] = True
        return out  # this state of the data already had its run
    out["due"] = True
    out["item"] = {
        "job": job,
        "run_key": run_key,
        "for_date": today.isoformat(),
        "trigger": TRIGGER,
        "payload": {"changes": changes, "target": key},
    }
    return out


def due(rt, cfg, now: datetime | None = None) -> dict[str, Any] | None:
    """The run to queue now for new data, if any (the scheduler, every tick)."""
    try:
        found = check(rt, cfg, now, remember=True)
    except Exception:
        log.exception("checking the advice inputs failed")
        return None
    return found["item"] if found["due"] else None


def pending(rt, day: date, now: datetime | None = None) -> dict[str, Any] | None:
    """For a read of the advice of ``day``: the changes since its newest run, whether they
    wait for the fixed run, and whether the limit stops a new version. ``None`` when the
    runner is not active, or ``day`` is not the current target day."""
    from vibesynergy.coach_runner import runner
    from vibesynergy.coach_runner import settings as runner_settings

    try:
        cfg = runner_settings.load(rt)
        if not (cfg.enabled and cfg.api_key_set) or runner.over_budget(rt, cfg, now):
            return None
        found = check(rt, cfg, now)
    except Exception:
        log.debug("reading the advice inputs failed", exc_info=True)
        return None
    if found["target"] != day or not found["changes"]:
        return None
    return found
