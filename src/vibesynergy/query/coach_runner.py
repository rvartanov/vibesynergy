"""What the portal shows and changes of the API coach runner (Coach → Settings).

The API key is write-only: ``overview`` says whether one is set, never what it is.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vibesynergy.coach_runner import JOBS, MODEL, MODELS, contact, prompts, registry, runner
from vibesynergy.coach_runner import schedule as runner_schedule
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.query import coach_replies
from vibesynergy.store.schema import coach_decision_log

SettingsError = runner_settings.RunnerSettingsError


def overview(rt) -> dict[str, Any]:
    cfg = runner_settings.load(rt)
    used = runner.usage(rt)
    cap = cfg.monthly_cap_usd
    return {
        "enabled": cfg.enabled,
        "api_key_set": cfg.api_key_set,
        "monthly_cap_usd": cap,
        "model": MODEL,
        "models": cfg.models,
        "tasks": cfg.tasks,
        "providers": list(runner_settings.PROVIDERS),
        "keys": cfg.keys,
        "local_url": cfg.local_url,
        "model_choices": list(MODELS),
        "max_turns": cfg.max_turns,
        "max_output_tokens": cfg.max_output_tokens,
        "usage": {**used, "share": round(used["cost_usd"] / cap, 4) if cap else None},
        "over_budget": used["cost_usd"] >= cap,
        "schedule": cfg.schedule,
        "advice": _advice(rt, cfg),
        # When an AI coach may reach the athlete (#119), for every AI coach.
        "contact": contact.state(rt),
        "timezone": runner.zone(rt).key,
        "jobs": list(JOBS),
        "last_runs": runner.last_runs(rt),
        "rules": {job: prompts.rules_for(job, prompts.athlete_name(rt.settings)) for job in JOBS},
        "athlete_name": prompts.athlete_name(rt.settings),
        # The harness task list: what each task loads first and how it starts.
        "registry": [
            {
                "id": task.id,
                "status": task.status,
                "runs": task.runs,
                "triggers": [trigger.kind for trigger in task.triggers],
                "context": list(registry.chain_for(task.id)),
                "approval": task.approval,
            }
            for task in registry.TASKS
        ],
    }


def _advice(rt, cfg) -> dict[str, Any]:
    """The advice during the day (#90): the daily maximum, how many ran today and what an
    update costs on average, so the page can show the cost of the maximum."""
    from vibesynergy.coach_runner import triggers

    most = triggers.max_runs(cfg)
    each = triggers.cost_per_run(rt)
    return {
        "max_runs": most,
        "max_runs_limit": triggers.MAX_RUNS_LIMIT,
        "runs_today": triggers.runs_today(rt),
        "cost_per_run_usd": each,
        "max_cost_per_day_usd": round(each * most, 2) if each is not None else None,
        "max_cost_per_month_usd": round(each * most * 30, 2) if each is not None else None,
        "debounce_minutes": int(triggers.DEBOUNCE.total_seconds() // 60),
    }


def save_settings(rt, changes: dict[str, Any]) -> dict[str, Any]:
    runner_settings.save(rt, changes)
    return overview(rt)


TestRunError = runner_schedule.TestRunError


def test_run(rt, job: str) -> dict[str, Any]:
    runner_schedule.enqueue_test(rt, job)
    return overview(rt)


def redo_insight(rt, activity_id: str) -> dict[str, Any]:
    """Write the public insight for one activity again (#158): a new entry with its own
    idempotency key; the newest wins and the earlier one stays in the coach log."""
    return runner_schedule.enqueue_insight_redo(rt, activity_id)


def resend(rt, job: str) -> dict[str, Any]:
    """Mail the newest report of ``job`` again (Coach → Settings → Last runs). Free."""
    from vibesynergy.coach_runner import report
    from vibesynergy.store.schema import coach_run

    with rt.engine.connect() as conn:
        run = (
            conn.execute(
                select(coach_run)
                .where(coach_run.c.job == job, coach_run.c.entry_id.is_not(None))
                .order_by(coach_run.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        entry = (
            conn.execute(
                select(coach_decision_log).where(coach_decision_log.c.id == run["entry_id"])
            )
            .mappings()
            .first()
            if run
            else None
        )
    if run is None or entry is None:
        raise TestRunError("There is no report to send again yet.")
    from vibesynergy.query import daily_advice

    state = daily_advice.version_state(rt, entry["id"])
    if state is not None and state["status"] != "current":
        # Only what is current is sent (#115): a replaced version is not mailed again.
        raise TestRunError("This report was replaced by a newer version; it is not sent again.")
    if not report.send(rt, job, run["for_date"], dict(entry), by_mail=True):
        raise TestRunError("Mail is not set up: see Settings → Alerts.")
    return overview(rt)


def set_key(rt, key: str, provider: str = "anthropic") -> dict[str, Any]:
    runner_settings.set_api_key(rt, key, provider=provider)
    return overview(rt)


def clear_key(rt, provider: str = "anthropic") -> dict[str, Any]:
    runner_settings.clear_api_key(rt, provider=provider)
    return overview(rt)


def report(rt, entry_id: int, tr=None) -> dict[str, Any]:
    """One coach entry with its replies, for the report page the mail links to. A daily
    advice has its one view (``advice_view``, #147): the page's title and header."""
    from vibesynergy.query.coach_workouts import _coach_row

    with rt.engine.connect() as conn:
        row = (
            conn.execute(select(coach_decision_log).where(coach_decision_log.c.id == entry_id))
            .mappings()
            .first()
        )
    if row is None:
        raise LookupError("no such coach entry")
    entry = _coach_row(dict(row))
    from vibesynergy.query import daily_advice

    # Where this report stands in its day thread (#115): a notice links here, and an
    # older version says it was replaced and by which one.
    thread = daily_advice.version_state(rt, entry_id)
    from vibesynergy.i18n import ENGLISH
    from vibesynergy.query import advice_view

    tr = tr or ENGLISH
    shown = advice_view.view(rt, dict(row), tr)
    entry["advice_view"] = shown
    # The sessions it lists, linked to their workout or activity (#181), read now.
    advice_view.with_session_links(rt, [entry])
    day = daily_advice.advice_day(dict(row)) if shown and not shown["second_opinion"] else None
    return {
        "entry": entry,
        "view": shown,
        # The day's versions, newest first, for "earlier versions" on the page.
        "versions": daily_advice.versions(daily_advice._entries(rt, day), tr) if day else [],
        "replies": coach_replies.for_entries(rt.engine, [entry_id])[entry_id],
        "thread": {**thread, "changed_because": daily_advice.changed_because(dict(row))}
        if thread
        else None,
    }


# The harness over MCP: the same tasks, runs and Test now as Coach → Settings. ------


def harness_tasks(rt) -> list[dict[str, Any]]:
    """Every registry task with its switch, schedule, provider, model and last run."""
    data = overview(rt)
    out = []
    for entry in data["registry"]:
        task = entry["id"]
        spec = data["tasks"].get(task) or {}
        sched = data["schedule"].get(task)
        run = data["last_runs"].get(task)
        out.append(
            {
                **entry,
                "on": sched["on"] if sched else spec.get("on", True),
                "schedule": sched,
                "provider": spec.get("provider"),
                "model": spec.get("model"),
                "last_run": run,
            }
        )
    return out


def harness_runs(rt, task: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """The run log, newest first: status, cost, what was preloaded, the contract result."""
    from vibesynergy.store.schema import coach_run

    limit = min(max(int(limit), 1), 50)
    stmt = select(coach_run).order_by(coach_run.c.started_at.desc(), coach_run.c.id.desc())
    if task:
        stmt = stmt.where(coach_run.c.job == task)
    with rt.engine.connect() as conn:
        rows = conn.execute(stmt.limit(limit)).mappings().all()
    return [
        {
            "id": r["id"],
            "task": r["job"],
            "run_key": r["run_key"],
            "for_date": r["for_date"].isoformat(),
            "trigger": r["trigger"],
            "status": r["status"],
            "model": r["model"],
            "started_at": r["started_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "turns": r["turns"],
            "cost_usd": round(r["cost_usd"], 4),
            "error": r["error"],
            "entry_id": r["entry_id"],
            "harness": r["harness"],
        }
        for r in rows
    ]
