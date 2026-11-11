"""What the portal shows and changes of the API coach runner (Coach → Settings).

The API key is write-only: ``overview`` says whether one is set, never what it is.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vibesynergy.coach_runner import JOBS, MODEL, MODELS, prompts, registry, runner
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


def save_settings(rt, changes: dict[str, Any]) -> dict[str, Any]:
    runner_settings.save(rt, changes)
    return overview(rt)


TestRunError = runner_schedule.TestRunError


def test_run(rt, job: str) -> dict[str, Any]:
    runner_schedule.enqueue_test(rt, job)
    return overview(rt)


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
    if not report.send(rt, job, run["for_date"], dict(entry)):
        raise TestRunError("Mail is not set up: see Settings → Alerts.")
    return overview(rt)


def set_key(rt, key: str, provider: str = "anthropic") -> dict[str, Any]:
    runner_settings.set_api_key(rt, key, provider=provider)
    return overview(rt)


def clear_key(rt, provider: str = "anthropic") -> dict[str, Any]:
    runner_settings.clear_api_key(rt, provider=provider)
    return overview(rt)


def report(rt, entry_id: int) -> dict[str, Any]:
    """One coach entry with its replies, for the report page the mail links to."""
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
    return {"entry": entry, "replies": coach_replies.for_entries(rt.engine, [entry_id])[entry_id]}


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
