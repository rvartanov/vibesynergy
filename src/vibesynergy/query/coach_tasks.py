"""The coach's task register: what the coach does, when, and whether it happened.

Since 10-11 the full list of coach tasks, with their switches, schedules and context, is
the AI harness registry (``coach_runner/registry.py``, Coach → Settings). This register
keeps the reports whose missing run can be told from the clock (morning, evening, the week
review) and the insight; a missed one alerts and
fires ``coach.run_missed`` (catch-up).

One list for every coach task: trigger, inputs, output, what the coach decides alone and
what needs the athlete's akkoord. ``status`` adds the last run of each task from the coach log
and the runs that are missing, so a skipped routine is noticed (the dashboard turns a
missing run into a notification). The prompts in ``docs/coach-prompts`` and the knowledge
base say how; this register says what and when, and a test keeps them in line.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, or_, select

from vibesynergy.store.schema import coach_decision_log

# How many past due moments are checked: recent misses alert, old history does not.
LOOKBACK_DAYS = 3


@dataclass(frozen=True)
class Task:
    id: str
    title: str  # English source string, translated in the portal
    trigger: str
    schedule: str  # daily, weekly (Sunday) or per_activity
    due: time | None  # local time by which the run's entry must exist (None: not checked)
    entry_type: str | None
    key_prefix: str | None  # idempotency_key prefix of the run's entry
    prompt: str | None  # docs/coach-prompts/<prompt>.md and KB coach-prompt-<prompt>
    inputs: tuple[str, ...]
    output: str
    decides: tuple[str, ...] = field(default=())
    asks: tuple[str, ...] = field(default=())


TASKS: tuple[Task, ...] = (
    Task(
        id="morning",
        title="Morning report",
        trigger="Coach via API, daily (Coach → Settings)",
        schedule="daily",
        due=time(11, 0),
        entry_type="morning",
        key_prefix="morning-",
        prompt="morning",
        inputs=(
            "vibesynergy_today",
            "vibesynergy_recovery",
            "vibesynergy_notes",
            "vibesynergy_injuries",
            "vibesynergy_plan_model",
            "vibesynergy_thresholds",
        ),
        output="one morning entry; new day-9 workouts",
        decides=("keep, adjust or skip today's session", "fill the plan up to day 9"),
        asks=("change a planned session", "change a rule or threshold"),
    ),
    Task(
        id="evening",
        title="Evening review",
        trigger="Coach via API, daily (Coach → Settings)",
        schedule="daily",
        due=time(23, 30),
        entry_type="evening",
        key_prefix="evening-",
        prompt="evening",
        inputs=("vibesynergy_activities", "vibesynergy_recovery", "vibesynergy_plan"),
        output="one evening entry with a recovery forecast for tomorrow",
        decides=("assess today's sessions",),
        asks=("delete duplicate activities",),
    ),
    Task(
        id="insight",
        title="Activity insights",
        trigger="Coach via API, when an activity is made public",
        schedule="per_activity",
        due=None,
        entry_type="activity_assessment",
        key_prefix="insight-",
        prompt="insight",
        inputs=("vibesynergy_activity", "vibesynergy_activity_insights"),
        output="one public insight per activity",
        decides=("publish the insight",),
    ),
    Task(
        id="weekly",
        title="Weekly review",
        trigger="Coach via API, Sunday (Coach → Settings)",
        schedule="weekly",
        due=time(23, 30),
        entry_type="note",
        key_prefix="weekreview:",
        prompt="weekly",
        inputs=(
            "vibesynergy_week",
            "vibesynergy_volume",
            "vibesynergy_coach_entries",
            "vibesynergy_weather",
        ),
        output="one weekly review note and a proposal for next week",
        asks=("next week's plan changes",),
    ),
)


def _due_days(task: Task, now_local: datetime) -> list[date]:
    """Days whose run should exist by now, newest first, within the lookback."""
    if task.due is None:
        return []
    out = []
    for back in range(LOOKBACK_DAYS):
        day = now_local.date() - timedelta(days=back)
        if task.schedule == "weekly" and day.weekday() != 6:
            continue
        if datetime.combine(day, task.due, now_local.tzinfo) <= now_local:
            out.append(day)
    return out


def _runs(engine: Engine, task: Task, since: date) -> tuple[datetime | None, set[date]]:
    """Last run of the task and the days it ran on since ``since``."""
    if task.entry_type is None:
        return None, set()
    where = [coach_decision_log.c.entry_type == task.entry_type]
    if task.entry_type == "note" and task.key_prefix:
        # The API runner keys its week review "weekly-<date>" (runner.entry_key); the old
        # routines used "weekreview:". Both count (8-11: a done review showed as skipped).
        prefixes = [task.key_prefix] + (["weekly-"] if task.id == "weekly" else [])
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


def status(engine: Engine, now: datetime, timezone: str) -> list[dict[str, Any]]:
    """Every task with its last run and the due runs that are missing."""
    now_local = now.astimezone(ZoneInfo(timezone))
    since = now_local.date() - timedelta(days=LOOKBACK_DAYS)
    out = []
    for task in TASKS:
        last, ran = _runs(engine, task, since)
        # A task that never ran is not set up here (no coach, or a new install): not missing.
        due = _due_days(task, now_local) if last is not None else []
        missing = [d.isoformat() for d in due if d not in ran]
        row = asdict(task)
        row["due"] = task.due.strftime("%H:%M") if task.due else None
        row["last_run"] = last.isoformat() if last else None
        row["missing"] = missing
        out.append(row)
    return out
