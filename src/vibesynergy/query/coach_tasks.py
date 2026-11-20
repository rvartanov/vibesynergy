"""The coach's task register: what each AI coach task does, when, and whether it happened.

Generated, never written by hand (#119). Every row comes from the AI harness registry
(``coach_runner/registry.py``: triggers, contract, context chain, what a task writes on its
own and what waits for the athlete's approval) and the settings (Coach → Settings: the
time, the days and the switch of each scheduled task, ``coach_runner.settings``). The
internal AI coach runs from the same two, and an external AI coach reads this register in
``vibesynergy_coach_handbook``, so both work to the same list and the same times.

What this module adds is the missed-run check. For the reports whose missing run can be
told from the clock (morning, evening, the week review) and the insight, ``status`` adds the
last run from the coach log and the runs that are missing. A run counts as missing once its
scheduled time plus ``MISSING_AFTER`` has passed (the same grace the scheduler gives a late
start, ``schedule.GRACE``), at the latest at the end of the day. A missed one alerts and
fires ``coach.run_missed`` (catch-up).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, or_, select

from vibesynergy.store.schema import coach_decision_log

# How many past due moments are checked: recent misses alert, old history does not.
LOOKBACK_DAYS = 3
# A scheduled run counts as missing this long after its time: the scheduler's grace
# (``coach_runner.schedule.GRACE``; a test keeps the two equal).
MISSING_AFTER = timedelta(hours=3)
END_OF_DAY = time(23, 59)


@dataclass(frozen=True)
class Tracked:
    """What the missed-run check needs beyond the registry: how the task's entry is keyed,
    and the tools its prompt reads first (a test holds them against the prompts)."""

    id: str
    key_prefix: str
    inputs: tuple[str, ...]


TRACKED: tuple[Tracked, ...] = (
    Tracked(
        "morning",
        "morning-",
        (
            "vibesynergy_today",
            "vibesynergy_recovery",
            "vibesynergy_notes",
            "vibesynergy_injuries",
            "vibesynergy_plan_model",
            "vibesynergy_thresholds",
        ),
    ),
    Tracked(
        "evening",
        "evening-",
        ("vibesynergy_activities", "vibesynergy_recovery", "vibesynergy_plan"),
    ),
    Tracked(
        "weekly",
        "weekreview:",
        (
            "vibesynergy_week",
            "vibesynergy_volume",
            "vibesynergy_coach_entries",
            "vibesynergy_weather",
        ),
    ),
    Tracked("insight", "insight-", ("vibesynergy_activity", "vibesynergy_activity_insights")),
)
BY_TRACKED = {t.id: t for t in TRACKED}


def schedule_for(rt: Any) -> dict[str, dict[str, Any]]:
    """The saved schedule (Coach → Settings), or the defaults where it cannot be read."""
    from vibesynergy.coach_runner import settings as runner_settings

    try:
        return runner_settings.load(rt).schedule
    except Exception:
        return runner_settings.default_schedule()


def _cadence(spec: dict[str, Any] | None, task: Any) -> str:
    if spec is None:
        events = [g.detail for g in task.triggers if g.kind == "event"]
        return "per_activity" if "activity.published" in events else "event"
    if "monthday" in spec:
        return "monthly"
    days = spec.get("days") or []
    if len(days) == 7:
        return "daily"
    return "weekly" if len(days) == 1 else "days"


def _missing_after(spec: dict[str, Any]) -> time:
    at = datetime.combine(date(2000, 1, 1), time.fromisoformat(spec["time"]))
    later = at + MISSING_AFTER
    return END_OF_DAY if later.date() != at.date() else later.time()


def register(
    schedule: dict[str, dict[str, Any]] | None = None,
    switches: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every harness task, in registry order, with its schedule from the settings.

    ``due`` is the scheduled time (``HH:MM``) of a task on a clock, ``None`` for a task
    started by an event. ``switches`` are the event tasks' on/off (``settings.tasks``)."""
    from vibesynergy.coach_runner import registry
    from vibesynergy.coach_runner import settings as runner_settings

    schedule = schedule if schedule is not None else runner_settings.default_schedule()
    switches = switches or {}
    out = []
    for task in registry.TASKS:
        spec = schedule.get(task.id) if registry.schedule_trigger(task.id) else None
        tracked = BY_TRACKED.get(task.id)
        triggers = []
        for trigger in task.triggers:
            item: dict[str, Any] = {"kind": trigger.kind, "detail": trigger.detail}
            if trigger.kind == "schedule" and spec is not None:
                item["when"] = registry.when(task.id, spec)
                item["time"] = spec["time"]
            triggers.append(item)
        if spec is not None:
            on = bool(spec.get("on"))
        else:
            on = task.status == "live" and (switches.get(task.id) or {}).get("on") is not False
        out.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "runs": task.runs,
                "on": on,
                "trigger": "; ".join(t.get("when") or t["detail"] for t in triggers),
                "triggers": triggers,
                "schedule": _cadence(spec, task),
                "due": spec["time"] if spec else None,
                "days": list(spec.get("days") or []) if spec and "monthday" not in spec else None,
                "monthday": spec.get("monthday") if spec else None,
                "missing_after": (
                    _missing_after(spec).strftime("%H:%M") if spec and tracked else None
                ),
                "entry_type": task.contract.entry_type,
                "key_prefix": tracked.key_prefix if tracked else None,
                "prompt": task.prompt or None,
                "inputs": list(tracked.inputs) if tracked else [],
                "context": list(registry.chain_for(task.id)) if task.runs == "model" else [],
                "contract": {
                    "entry_type": task.contract.entry_type,
                    "required": list(task.contract.required),
                    "max_words": task.contract.max_words,
                    "safety_required": task.contract.safety,
                    "target_day": task.contract.target or None,
                },
                "writes": list(task.writes),
                "approval": list(task.approval),
                "scopes": list(task.scopes),
                "requires": list(task.requires),
                "notes": task.notes,
            }
        )
    return out


def _due_days(row: dict[str, Any], now_local: datetime) -> list[date]:
    """Days whose run should exist by now, newest first, within the lookback."""
    if row["due"] is None or row["missing_after"] is None:
        return []
    after = time.fromisoformat(row["missing_after"])
    days = row["days"]
    out = []
    for back in range(LOOKBACK_DAYS):
        day = now_local.date() - timedelta(days=back)
        if days is not None and day.weekday() not in days:
            continue
        if row["monthday"] is not None and day.day != row["monthday"]:
            continue
        if datetime.combine(day, after, now_local.tzinfo) <= now_local:
            out.append(day)
    return out


def _runs(
    engine: Engine, task_id: str, entry_type: str | None, since: date
) -> tuple[datetime | None, set[date]]:
    """Last run of a tracked task and the days it ran on since ``since``."""
    tracked = BY_TRACKED.get(task_id)
    if entry_type is None or tracked is None:
        return None, set()
    # A second opinion from an advisory AI coach (#118) is no run of the task.
    role = func.json_extract(coach_decision_log.c.meta, "$.harness.role")
    where = [
        coach_decision_log.c.entry_type == entry_type,
        or_(role.is_(None), role != "advisory"),
    ]
    if entry_type == "note":
        # The API runner keys its week review "weekly-<date>" (runner.entry_key); the old
        # routines used "weekreview:". Both count (8-11: a done review showed as skipped).
        prefixes = [tracked.key_prefix] + (["weekly-"] if task_id == "weekly" else [])
        where.append(or_(*(coach_decision_log.c.idempotency_key.like(f"{p}%") for p in prefixes)))
    with engine.connect() as conn:
        last = conn.execute(select(func.max(coach_decision_log.c.at)).where(*where)).scalar()
        days = conn.execute(
            select(coach_decision_log.c.for_date)
            .where(*where, coach_decision_log.c.for_date >= since)
            .distinct()
        ).scalars()
        ran = {d if isinstance(d, date) else date.fromisoformat(d) for d in days if d}
    return last, ran


def _last_by_task(engine: Engine) -> dict[str, datetime]:
    """The newest entry per harness task (``meta.harness.task``), for the other tasks."""
    task = func.json_extract(coach_decision_log.c.meta, "$.harness.task")
    with engine.connect() as conn:
        rows = conn.execute(
            select(task, func.max(coach_decision_log.c.at)).where(task.is_not(None)).group_by(task)
        ).all()
    return {str(name): at for name, at in rows if name}


def status(
    engine: Engine,
    now: datetime,
    timezone: str,
    schedule: dict[str, dict[str, Any]] | None = None,
    switches: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every task with its last run and, for the tracked ones, the due runs that are
    missing. ``schedule`` is the saved one (``schedule_for``); without it, the defaults."""
    now_local = now.astimezone(ZoneInfo(timezone))
    since = now_local.date() - timedelta(days=LOOKBACK_DAYS)
    others = _last_by_task(engine)
    out = []
    for row in register(schedule, switches):
        if row["id"] in BY_TRACKED:
            last, ran = _runs(engine, row["id"], row["entry_type"], since)
            # A task that never ran is not set up here (no coach, a new install): not missing.
            due = _due_days(row, now_local) if last is not None else []
            missing = [d.isoformat() for d in due if d not in ran]
        else:
            last, missing = others.get(row["id"]), []
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        row["last_run"] = last.isoformat() if last else None
        row["missing"] = missing
        out.append(row)
    return out


def status_for(rt: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """``status`` with this install's saved schedule and switches (Coach → Settings)."""
    from vibesynergy.coach_runner import settings as runner_settings
    from vibesynergy.core.timeutil import utc_now

    try:
        cfg = runner_settings.load(rt)
        schedule, switches = cfg.schedule, cfg.tasks
    except Exception:
        schedule, switches = runner_settings.default_schedule(), {}
    return status(rt.engine, now or utc_now(), rt.settings.timezone, schedule, switches)
