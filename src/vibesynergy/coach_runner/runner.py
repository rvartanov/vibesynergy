"""One coach run: prompt, tool loop, budget, report, audit.

A run is one row in ``coach_run`` keyed by (job, run_key): the local date for the morning,
evening and weekly jobs, the event id for an insight. A finished row is never run again; a
crash or a transient API error leaves it retryable once (``MAX_ATTEMPTS``). Usage and cost
are written after every model turn, so the monthly total also counts a run that died.

Budget: ``max_turns`` and ``max_output_tokens`` per run, and the monthly cap. The cap is
checked before every turn; at 80 % and 100 % of it VibeSynergy warns once per month by mail
and in the log, and at 100 % runs stop until the next month or a higher cap.

Logging: INFO only says which job ran, how it ended, tokens and cost. Prompts, replies,
tool arguments and results never go to the log or the audit log.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update

from vibesynergy import alerts, i18n
from vibesynergy.coach_runner import (
    MODEL,
    REPORT_JOBS,
    contact,
    context,
    contract,
    pricing,
    prompts,
    registry,
    report,
)
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.coach_runner.model import (
    AnthropicClient,
    ModelClient,
    ModelError,
    OpenAICompatClient,
    request_options,
)
from vibesynergy.coach_runner.model import capabilities as model_capabilities
from vibesynergy.coach_runner.tools import (
    COACH_SCOPES,
    INSIGHT_SCOPES,
    INSIGHT_TOOLS,
    ToolBox,
    insight_guard,
    kb_guard,
)
from vibesynergy.core.timeutil import utc_now
from vibesynergy.core.user_datetime import format_user_date
from vibesynergy.query import coach_replies
from vibesynergy.store.schema import audit_log, coach_decision_log, coach_run

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
ZONE = "Europe/Amsterdam"
WARN_AT = (80, 100)
ACTOR = "coach_runner"
RUNNER_AUTHOR = "client:coach-runner"
ENTRY_TYPES = registry.entry_types()


class RunnerError(Exception):
    """A run failed in a way the job queue should retry (``retry``) or not."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass
class RunResult:
    status: str  # done, failed, budget, off, skipped
    run_id: int | None = None
    entry_id: int | None = None
    cost_usd: float = 0.0
    turns: int = 0
    stop: str | None = None
    error: str | None = None
    mailed: bool = False


def zone(rt) -> ZoneInfo:
    """The athlete's own time zone: the schedule, "today" and the budget month follow it."""
    configured = (rt.settings.coach_runner or {}).get("timezone")
    return ZoneInfo(str(configured or getattr(rt.settings, "timezone", None) or ZONE))


def month_start(rt, now: datetime) -> datetime:
    local = now.astimezone(zone(rt))
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(UTC)


def month_key(rt, now: datetime) -> str:
    return now.astimezone(zone(rt)).strftime("%Y-%m")


def usage(rt, now: datetime | None = None) -> dict[str, Any]:
    """Runs, tokens and estimated cost since the first of this (local) month."""
    now = now or utc_now()
    with rt.engine.connect() as conn:
        row = conn.execute(
            select(
                func.count(),
                func.coalesce(func.sum(coach_run.c.input_tokens), 0),
                func.coalesce(func.sum(coach_run.c.output_tokens), 0),
                func.coalesce(func.sum(coach_run.c.cache_write_tokens), 0),
                func.coalesce(func.sum(coach_run.c.cache_read_tokens), 0),
                func.coalesce(func.sum(coach_run.c.cost_usd), 0.0),
            ).where(coach_run.c.started_at >= month_start(rt, now), coach_run.c.turns > 0)
        ).one()
    return {
        "month": month_key(rt, now),
        "runs": int(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "cache_write_tokens": int(row[3]),
        "cache_read_tokens": int(row[4]),
        "cost_usd": round(float(row[5]), 4),
    }


def over_budget(rt, cfg, now: datetime | None = None) -> bool:
    return usage(rt, now)["cost_usd"] >= cfg.monthly_cap_usd


def check_budget(rt, cfg, now: datetime | None = None) -> int | None:
    """Warn once per month when spend passes 80 % and 100 % of the cap. Returns the level
    warned now, if any."""
    now = now or utc_now()
    spent = usage(rt, now)["cost_usd"]
    share = spent / cfg.monthly_cap_usd * 100 if cfg.monthly_cap_usd else 100
    reached = [level for level in WARN_AT if share >= level]
    if not reached:
        return None
    level = max(reached)
    state = runner_settings.state(rt)
    month = month_key(rt, now)
    warned = dict(state.get("warned") or {})
    if level in (warned.get(month) or []):
        return None
    warned[month] = sorted({*(warned.get(month) or []), *reached})
    state["warned"] = {k: v for k, v in warned.items() if k >= month}  # keep this month
    runner_settings.save_state(rt, state)
    log.warning(
        "coach runner: %d%% of the monthly limit used ($%.2f of $%.2f)",
        level,
        spent,
        cfg.monthly_cap_usd,
    )
    tr = i18n.translator(report.language(rt.settings))
    if level >= 100:
        subject = tr("Coach via API stopped: monthly limit reached")
        text = tr(
            "Your AI coach used ${spent} of the ${cap} monthly limit and pauses until next month. "
            "Raise the limit in Coach → Settings to continue now.",
            spent=f"{spent:.2f}",
            cap=f"{cfg.monthly_cap_usd:.2f}",
        )
    else:
        subject = tr("Coach via API: {level}% of the monthly limit used", level=level)
        text = tr(
            "Your AI coach used ${spent} of the ${cap} monthly limit. It keeps running until the "
            "limit is reached.",
            spent=f"{spent:.2f}",
            cap=f"{cfg.monthly_cap_usd:.2f}",
        )
    link = alerts.portal_url(rt, "#/coach/settings")
    alerts.deliver(rt, subject, f"{text}\n\n{link}".strip(), only=["email"])
    return level


# --- the run row ---------------------------------------------------------------------------


def get_run(rt, job: str, run_key: str) -> dict[str, Any] | None:
    with rt.engine.connect() as conn:
        row = conn.execute(
            select(coach_run).where(coach_run.c.job == job, coach_run.c.run_key == run_key)
        ).mappings()
        found = row.first()
        return dict(found) if found else None


def model_for(cfg, job: str) -> str:
    """The model chosen in Coach → Settings for this task."""
    return cfg.tasks[job]["model"] if job in cfg.tasks else cfg.models["coach"]


def provider_for(cfg, job: str) -> str:
    return cfg.tasks[job]["provider"] if job in cfg.tasks else "anthropic"


def _claim(rt, job: str, run_key: str, for_date: date, trigger: str) -> tuple[dict, bool]:
    """(row, may_run). Creates the row, or takes a retryable one for another attempt."""
    now = utc_now()
    with rt.engine.begin() as conn:
        row = (
            conn.execute(
                select(coach_run).where(coach_run.c.job == job, coach_run.c.run_key == run_key)
            )
            .mappings()
            .first()
        )
        if row is None:
            run_id = conn.execute(
                insert(coach_run)
                .values(
                    job=job,
                    run_key=run_key,
                    for_date=for_date,
                    trigger=trigger,
                    status="running",
                    attempts=1,
                    model=model_for(runner_settings.load(rt), job),
                    started_at=now,
                    updated_at=now,
                    turns=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_write_tokens=0,
                    cache_read_tokens=0,
                    cost_usd=0.0,
                )
                .returning(coach_run.c.id)
            ).scalar_one()
            fresh = conn.execute(select(coach_run).where(coach_run.c.id == run_id)).mappings()
            return dict(fresh.one()), True
        row = dict(row)
        if row["status"] in ("done", "skipped"):
            return row, False
        budget_retry = row["status"] == "budget"
        if not budget_retry and row["attempts"] >= MAX_ATTEMPTS:
            if row["status"] != "failed":  # a crash on the last attempt
                conn.execute(
                    update(coach_run)
                    .where(coach_run.c.id == row["id"])
                    .values(
                        status="failed",
                        finished_at=now,
                        updated_at=now,
                        error=row["error"] or "stopped during its last attempt",
                    )
                )
            return row, False
        conn.execute(
            update(coach_run)
            .where(coach_run.c.id == row["id"])
            .values(
                status="running",
                attempts=row["attempts"] if budget_retry else row["attempts"] + 1,
                trigger=trigger,
                updated_at=now,
                error=None,
            )
        )
        row.update(status="running")
        return row, True


def _save(rt, run_id: int, **values: Any) -> None:
    values.setdefault("updated_at", utc_now())
    with rt.engine.begin() as conn:
        conn.execute(update(coach_run).where(coach_run.c.id == run_id).values(**values))


def _audit(rt, run: dict[str, Any], result: RunResult) -> None:
    try:
        with rt.engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(),
                    actor=ACTOR,
                    action="coach_runner.run",
                    detail={
                        "job": run["job"],
                        "run_key": run["run_key"],
                        "trigger": run["trigger"],
                        "model": run.get("model") or MODEL,
                        "status": result.status,
                        "turns": result.turns,
                        "stop": result.stop,
                        "cost_usd": round(result.cost_usd, 4),
                        "entry_id": result.entry_id,
                        "mailed": result.mailed,
                    },
                    client_id="coach-runner",
                    author=RUNNER_AUTHOR,
                    target=f"coach_run:{run['id']}",
                    outcome=(f"{result.status}: {result.error}" if result.error else result.status)[
                        :300
                    ],
                )
            )
    except Exception:
        log.exception("could not write the coach runner audit row")


# --- prompt and context ----------------------------------------------------------------------


def system_blocks(rt, job: str) -> list[dict[str, Any]]:
    """The stable part: the MCP guide and the job prompt, cached with the tool definitions
    (tools render first, so the breakpoint at the end of the system covers both)."""
    from vibesynergy.mcp.server import GUIDE

    return [
        {"type": "text", "text": GUIDE},
        {
            "type": "text",
            "text": prompts.job_prompt(rt.settings, job, rt.engine),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _reply_lines(replies: list[dict[str, Any]], name: str = prompts.DEFAULT_NAME) -> str:
    if not replies:
        return f"Antwoorden van {name} sinds de vorige run: geen."
    lines = [f"Antwoorden van {name} sinds de vorige run (oudste eerst):"]
    for reply in replies:
        when = reply["at"][:16].replace("T", " ")
        about = reply["entry_first_line"].replace('"', "'")
        lines.append(
            f'- antwoord #{reply["id"]} (approval_id "reply:{reply["id"]}" als het een '
            f"akkoord is) op entry #{reply['entry_id']} ({reply['entry_type']}, "
            f"{reply['for_date']}, "
            f'"{about}") om {when} UTC:\n  """{reply["text"]}"""'
        )
    return "\n".join(lines)


def entry_key(job: str, for_date: date, run_key: str | None = None) -> str:
    """The entry's idempotency key. A scheduled run keys on the day; a test or a redo
    (Coach → Settings) gets its own, else it would get the day's earlier entry back."""
    key = f"{job}-{for_date.isoformat()}"
    if run_key and run_key != for_date.isoformat():
        key += f"-{run_key}"
    return key


def insight_key(activity_id: str, run_key: str | None = None, *, redo: bool = False) -> str:
    """The insight entry's idempotency key: ``insight-<id>``, and for a redo (#158) with
    the redo's run key, so the new entry does not get the earlier one back."""
    key = f"insight-{activity_id}"
    return f"{key}-{run_key}" if redo and run_key else key


def _insight_lines(payload: dict[str, Any], run_key: str | None) -> str:
    """The keys the insight writes with, and for a redo that it replaces an existing one."""
    ids = [str(i) for i in payload.get("activity_ids") or []]
    if not ids:
        return ""
    redo = bool(payload.get("redo"))
    keys = ", ".join(
        f'activity_id "{i}": idempotency_key "{insight_key(i, run_key, redo=redo)}"' for i in ids
    )
    lines = [f"Schrijf per activiteit met deze idempotency_key: {keys}."]
    if redo:
        replaces = payload.get("replaces")
        which = f" (entry #{replaces})" if replaces else ""
        lines.append(
            f"Dit is een redo: deze activiteit heeft al een inzicht{which}, en de atleet vraagt "
            "een nieuw inzicht. Sla haar dus niet over omdat er al een is (ook al staat ze in "
            "vibesynergy_activity_insights als present): schrijf een vervangend inzicht met de "
            "idempotency_key hierboven. Het nieuwste inzicht telt; het vorige blijft in het "
            "log staan."
        )
    return "\n" + "\n".join(lines)


def start_message(
    rt,
    job: str,
    for_date: date,
    *,
    replies: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    run_key: str | None = None,
    harness: dict[str, Any] | None = None,
) -> str:
    """The first user turn. ``harness``, when given, receives the preloaded block report."""
    if job not in registry.BY_ID:
        raise RunnerError(f"{job} is not a harness task", retry=False)
    iso_year, iso_week, _ = for_date.isocalendar()
    head = (
        f"Run: {job}. Datum: {format_user_date(for_date)} ({for_date.isoformat()}), "
        f"tijdzone {zone(rt).key}, ISO-week {iso_year}-W{iso_week:02d}."
    )
    if job == "insight":
        p = payload or {}
        ids = ",".join(p.get("activity_ids") or [])
        fire = f"event={p.get('event_type', 'activity.published')} event_id={p.get('event_id', '')}"
        return (
            f"{head}\n<routine-fire-payload>{fire} activity_ids={ids}</routine-fire-payload>"
            + _insight_lines(p, run_key)
            + _preloaded(rt, job, for_date, harness, activity_ids=p.get("activity_ids") or [])
        )
    key = entry_key(job, for_date, run_key)
    entry = ENTRY_TYPES[job]
    replied = _reply_lines(replies or [], prompts.athlete_name(rt.settings))
    return (
        f'{head}\nSchrijf je entry met entry_type "{entry}", idempotency_key "{key}" en '
        f'for_date "{for_date.isoformat()}".'
        + _advice_lines(rt, job, for_date, payload, key)
        + f"\n\n{replied}"
        + _about(payload)
        + _preloaded(
            rt, job, for_date, harness, activity_ids=(payload or {}).get("activity_ids") or []
        )
    )


CHANGE_NAMES = {
    "sleep": "slaap",
    "hrv": "HRV",
    "checkin": "de check-in",
    "note": "een notitie",
    "injury": "pijn of een blessure",
    "activity": "een training (gedaan of gewijzigd)",
    "plan": "het plan",
    "reply": "een antwoord van {name}",
}


def _advice_lines(
    rt, job: str, for_date: date, payload: dict[str, Any] | None, key: str | None = None
) -> str:
    """For a daily advice: the day it is for, and for a new version what came in (#90)."""
    target = _advice_target(job, for_date)
    if target is None:
        return ""
    lines = [
        f"Dit advies (kleur, safety, sessies) geldt voor {format_user_date(target)} "
        f"({target.isoformat()})."
    ]
    changes = (payload or {}).get("changes") or []
    if changes:
        name = prompts.athlete_name(rt.settings)
        what = ", ".join(CHANGE_NAMES.get(c, c).format(name=name) for c in changes)
        lines.append(
            f"Dit is een nieuwe versie van het huidige advies: sinds de vorige versie kwam "
            f"nieuw binnen: {what}. Lees dat eerst. Schrijf het hele advies opnieuw; het "
            "vervangt de vorige versie. Verandert er niets aan kleur, veiligheid of "
            "sessies, zeg dat dan kort."
        )
    previous = _previous(rt, target, key)
    if previous is not None:
        # The day thread (#115): the version this one replaces, named, so the model does
        # not have to find it.
        when = str(previous.get("at") or "")[11:16]
        lines.append(
            f"De huidige versie voor die dag is advies #{previous['id']} (versie "
            f'{previous.get("version")}, {when} UTC): "{previous["advice"][:200]}". Jouw '
            "entry is de volgende versie: bouw erop voort (blok day_thread) en geef "
            f'changed_because = {{"previous": {previous["id"]}, "reason": "wat verandert '
            'tegenover die versie en waarom"}.'
        )
    return "\n" + "\n".join(lines)


def _previous(rt, target: date, key: str | None = None) -> dict[str, Any] | None:
    """The day's current advice version, which this run's advice replaces (#115). ``key``
    is the run's own entry key: an entry an earlier attempt of this run wrote is not it."""
    from vibesynergy.query import daily_advice

    try:
        return daily_advice.previous_version(rt, target, key=key, author=RUNNER_AUTHOR)
    except Exception:
        log.exception("coach runner: reading the day thread failed")
        return None


def _about(payload: dict[str, Any] | None) -> str:
    """What an event task is about (a test session and its activities), for the model."""
    p = payload or {}
    if not p.get("session_id"):
        return ""
    ids = ", ".join(p.get("activity_ids") or [])
    return f"\n\nGeplande sessie: {p['session_id']}. Activiteiten van die dag: {ids}."


def _preloaded(
    rt, job: str, for_date: date, harness: dict[str, Any] | None = None, **extra: Any
) -> str:
    """The task's context chain, loaded before the first turn so the model starts
    with what it always needs. Tools stay available for anything beyond it."""
    found = context.preload(rt, job, for_date, **extra)
    if harness is not None:
        harness.update(context=found["version"], blocks=found["blocks"])
    return "\n\nVooraf geladen context (stand van nu; haal meer op met de tools):\n" + found["text"]


def contract_issues(
    job: str,
    entry: dict[str, Any] | None,
    for_date: date | None = None,
    previous: int | None = None,
) -> list[str]:
    """Where the entry a run wrote misses the task's output contract. Any issue fails the
    run (fail closed, #88): the entry is marked invalid and is not mailed.

    The write itself was already checked (``contract.check_write`` refuses a bad entry back
    to the model, which can then repair it). This is the second net, for what only the run
    knows: the entry is of the task's type, for the run's day, within the task's length,
    written under this harness, and a daily advice carries its safety outcome and, when
    the day already had a version at the start of the run (``previous``), builds on the
    day thread (``changed_because``, #115)."""
    task = registry.BY_ID.get(job)
    if task is None or entry is None:
        return []
    issues = []
    want = task.contract.entry_type
    if want and entry.get("entry_type") != want:
        issues.append(f"entry_type {entry.get('entry_type')} instead of {want}")
    text = str(entry.get("text") or "")
    if "summary" in task.contract.required and not text.strip():
        issues.append("no summary")
    limit = task.contract.max_words
    words = len(text.split())
    if limit and words > limit * contract.WORD_SLACK:
        issues.append(f"{words} words, contract is {limit}")
    written_for = entry.get("for_date")
    if "for_date" in task.contract.required and written_for is None:
        issues.append("no for_date")
    elif for_date is not None and written_for is not None and written_for != for_date:
        issues.append(f"for_date {written_for} instead of {for_date}")
    meta = entry.get("meta") or {}
    stamp = meta.get("harness") or {}
    if stamp.get("via") != "runner" or stamp.get("task") != job:
        issues.append("not written under this run's harness")
    if task.contract.safety:
        safety = meta.get("safety")
        if not isinstance(safety, dict) or safety.get("level") not in contract.LEVELS:
            issues.append("no safety outcome (level, colour, reasons)")
        if previous is not None and not (meta.get("changed_because") or {}).get("reason"):
            issues.append(f"ignores the day thread: no changed_because for advice #{previous}")
    return issues


def _mark_invalid(rt, entry_id: int, issues: list[str]) -> None:
    """An entry that failed the contract stays in the log, marked: nothing downstream
    (the mail, the dashboard, #89's current advice) may treat it as valid."""
    with rt.engine.begin() as conn:
        meta = conn.execute(
            select(coach_decision_log.c.meta).where(coach_decision_log.c.id == entry_id)
        ).scalar()
        merged = dict(meta or {})
        merged["harness"] = {**(merged.get("harness") or {}), "valid": False, "issues": issues}
        conn.execute(
            update(coach_decision_log)
            .where(coach_decision_log.c.id == entry_id)
            .values(meta=merged)
        )


def find_entry(
    rt, job: str, for_date: date, since: datetime, run_key: str | None = None
) -> dict[str, Any] | None:
    """The entry this run wrote: by its idempotency key, else the runner's newest entry of
    that type since the run started."""
    key = entry_key(job, for_date, run_key)
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                # Its own entry only: a key belongs to its author (#118), and a key
                # another AI coach used first is stored for the runner with its suffix.
                select(coach_decision_log).where(
                    coach_decision_log.c.idempotency_key.in_((key, f"{key}-coach-runner")),
                    coach_decision_log.c.author == RUNNER_AUTHOR,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            row = (
                conn.execute(
                    select(coach_decision_log)
                    .where(
                        coach_decision_log.c.author == RUNNER_AUTHOR,
                        coach_decision_log.c.entry_type == ENTRY_TYPES[job],
                        coach_decision_log.c.at >= since,
                    )
                    .order_by(coach_decision_log.c.id.desc())
                )
                .mappings()
                .first()
            )
    return dict(row) if row else None


# --- the run -------------------------------------------------------------------------------


def _client(rt, client: ModelClient | None, provider: str = "anthropic") -> ModelClient:
    """The model client for the task's provider."""
    if client is not None:
        return client
    key = runner_settings.api_key(rt, provider)
    if provider == "anthropic":
        if not key:
            raise RunnerError(f"no {provider} API key", retry=False)
        return AnthropicClient(key)
    if provider == "local":
        url = runner_settings.load(rt).local_url
        if not url:
            raise RunnerError("no address for the local server", retry=False)
        return OpenAICompatClient(url, key)
    if not key:
        raise RunnerError(f"no {provider} API key", retry=False)
    return OpenAICompatClient(runner_settings.PROVIDER_URLS[provider], key)


def run(
    rt,
    job: str,
    run_key: str,
    for_date: date,
    *,
    trigger: str = "schedule",
    payload: dict[str, Any] | None = None,
    client: ModelClient | None = None,
) -> RunResult:
    cfg = runner_settings.load(rt)
    if not cfg.enabled or not cfg.api_key_set:
        return RunResult(status="off")
    if job not in registry.live():
        # No model call outside the registry (#88): a job without a task has no prompt
        # version, no context chain and no contract, so it does not run.
        log.warning("coach runner: %s is not a live harness task; not run", job)
        return RunResult(status="failed", error=f"{job} is not a harness task")
    if over_budget(rt, cfg):
        check_budget(rt, cfg)
        existing = get_run(rt, job, run_key)
        if existing is None:
            row, _ = _claim(rt, job, run_key, for_date, trigger)
            _save(rt, row["id"], status="budget", stop_reason="monthly_cap")
            result = RunResult(status="budget", run_id=row["id"], stop="monthly_cap")
            _audit(rt, {**row, "trigger": trigger}, result)
            return result
        if existing["status"] in ("running", "failed") and existing["attempts"] < MAX_ATTEMPTS:
            _save(rt, existing["id"], status="budget", stop_reason="monthly_cap")
            return RunResult(status="budget", run_id=existing["id"], stop="monthly_cap")
        return RunResult(status=existing["status"], run_id=existing["id"])
    row, may_run = _claim(rt, job, run_key, for_date, trigger)
    if not may_run:
        return RunResult(status="skipped", run_id=row["id"], entry_id=row.get("entry_id"))
    started = utc_now()
    log.info("coach runner: %s %s started (attempt %d)", job, run_key, row["attempts"])
    try:
        result = _loop(rt, cfg, row, job, for_date, payload, client, started)
    except RunnerError as exc:
        _save(rt, row["id"], status="failed", error=str(exc)[:500], finished_at=utc_now())
        failed = get_run(rt, job, run_key) or row
        result = RunResult(
            status="failed",
            run_id=row["id"],
            cost_usd=failed["cost_usd"],
            turns=failed["turns"],
            error=str(exc),
        )
        _audit(rt, row, result)
        log.warning("coach runner: %s %s failed: %s", job, run_key, exc)
        check_budget(rt, cfg)
        if exc.retry and row["attempts"] < MAX_ATTEMPTS:
            raise
        notify_failure(rt, job, for_date, result.error or "")
        return result
    except Exception as exc:
        # Anything unexpected (a malformed answer from a local model server, a locked
        # database): the run must not stay "running", or today's run is never tried again
        # and nobody hears about it. The job queue retries; the last attempt tells the athlete.
        message = f"{type(exc).__name__}: {exc}"[:500]
        with contextlib.suppress(Exception):
            _save(rt, row["id"], status="failed", error=message, finished_at=utc_now())
        log.exception("coach runner: %s %s failed unexpectedly", job, run_key)
        if row["attempts"] >= MAX_ATTEMPTS:
            with contextlib.suppress(Exception):
                notify_failure(rt, job, for_date, message)
        raise
    _audit(rt, row, result)
    if result.status == "failed":
        notify_failure(rt, job, for_date, result.error or "")
    log.info(
        "coach runner: %s %s %s after %d turns, $%.4f",
        job,
        run_key,
        result.status,
        result.turns,
        result.cost_usd,
    )
    check_budget(rt, cfg)
    return result


FAILED_SUBJECT = "Coach report failed: {job}, {day}"
FAILED_TEXT = (
    "Your AI coach could not finish the {job} report for {day}, so no report came. "
    "Try again with Test now under Coach → Settings."
)
JOB_NAMES = {
    "morning": "Morning advice",
    "evening": "Evening review",
    "weekly": "Week review",
    "monthly": "Month report",
    "block": "Block review",
    "race": "Race brief",
    "test": "Test evaluation",
    "thresholds": "Threshold check",
    "kb": "Knowledge base care",
    "injury": "Injury check",
    "checkin": "Check-in reaction",
}


def notify_failure(rt, job: str, for_date: date, error: str) -> list[str]:
    """when something goes wrong I must get a message. A report run that
    ends without a report sends one, on mail and push, with what went wrong in short."""
    if job not in JOB_NAMES:
        return []  # the insight line has no report to miss
    from vibesynergy.core.user_datetime import format_user_short_day

    lang = report.language(rt.settings)
    tr = i18n.translator(lang)
    day = format_user_short_day(for_date, lang)
    name = tr(JOB_NAMES[job])
    text = tr(FAILED_TEXT, job=name.lower(), day=day)
    if error:
        text += "\n\n" + tr("What went wrong: {error}", error=error[:300])
    link = alerts.portal_url(rt, "#/coach/settings")
    try:
        if not contact.allowed(rt):
            # Outside the contact window (#119) the notice waits until it opens.
            contact.hold_message(
                rt,
                key=f"failed:{job}:{for_date.isoformat()}",
                subject=tr(FAILED_SUBJECT, job=name, day=day),
                body=f"{text}\n\n{link}".strip(),
                only=["email", "push"],
            )
            return []
        return alerts.deliver(
            rt,
            tr(FAILED_SUBJECT, job=name, day=day),
            f"{text}\n\n{link}".strip(),
            only=["email", "push"],
        )
    except Exception:  # never let the warning hide the failure itself
        log.exception("coach runner: warning about the failed %s run failed", job)
        return []


def _loop(rt, cfg, row, job, for_date, payload, client, started) -> RunResult:
    provider = provider_for(cfg, job)
    model = _client(rt, client, provider)
    model_name = row.get("model") or model_for(cfg, job)
    if not registry.model_ok(job, model_capabilities(provider, model_name)):
        raise RunnerError(f"{model_name} lacks what {job} needs", retry=False)
    # What this run is told and given, as one stamp; every entry it writes carries it.
    stamp = prompts.harness_stamp(rt.settings, job, rt.engine)
    under: dict[str, Any] = {"task": job, "for_date": for_date, "stamp": stamp}
    advice_target = _advice_target(job, for_date)
    if advice_target is not None:
        # A daily advice (#90): the day it is for, and why this version is made.
        under.update(
            target=advice_target,
            trigger=row.get("trigger"),
            changes=list((payload or {}).get("changes") or []),
        )
    if job == "insight":
        ids = (payload or {}).get("activity_ids") or []
        box = ToolBox(
            rt,
            INSIGHT_SCOPES,
            only=INSIGHT_TOOLS,
            # A redo (#158) writes with its own key: the run's key would otherwise
            # return the earlier insight unchanged.
            guard=insight_guard(
                rt.engine,
                ids,
                keys=[insight_key(i, row["run_key"], redo=True) for i in ids]
                if (payload or {}).get("redo")
                else None,
            ),
            run=under,
        )
        replies: list[dict[str, Any]] = []
    else:
        box = ToolBox(
            rt,
            COACH_SCOPES | set(registry.BY_ID[job].scopes),
            guard=kb_guard() if job == "kb" else None,
            run=under,
        )
        replies = coach_replies.unread(rt.engine)
    try:
        system = system_blocks(rt, job)
    except prompts.PromptError as exc:
        raise RunnerError(str(exc), retry=False) from exc
    tools = box.definitions()
    harness: dict[str, Any] = {
        "version": contract.HARNESS_VERSION,
        "stamp": stamp,
        "rules": prompts.RULES_VERSION,
        "prompt": prompts.prompt_version(rt.settings, job, rt.engine),
    }
    if advice_target is not None:
        # The inputs this version starts from: new data after this is a change (#90).
        from vibesynergy.coach_runner import triggers

        harness["target"] = advice_target.isoformat()
        harness["changes"] = under["changes"]
        before = _previous(rt, advice_target, entry_key(job, for_date, row["run_key"]))
        harness["previous"] = before["id"] if before else None
        try:
            harness["inputs"] = triggers.fingerprint(rt, advice_target)
        except Exception:
            log.exception("coach runner: reading the advice inputs failed")
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": start_message(
                rt,
                job,
                for_date,
                replies=replies,
                payload=payload,
                run_key=row["run_key"],
                harness=harness,
            ),
        }
    ]
    _save(rt, row["id"], harness=harness)
    totals = {k: row[f"{k}_tokens"] for k in ("input", "output", "cache_write", "cache_read")}
    cost = float(row["cost_usd"])
    turns = int(row["turns"])
    month_before = usage(rt)["cost_usd"] - cost  # other runs this month
    stop: str | None = None
    for _ in range(cfg.max_turns):
        left = cfg.max_output_tokens - totals["output"]
        if left <= 0:
            stop = "max_output_tokens"
            break
        if month_before + cost >= cfg.monthly_cap_usd:
            stop = "monthly_cap"
            break
        request = {
            "model": model_name,
            "max_tokens": min(runner_settings.RESPONSE_MAX_TOKENS, left),
            "system": system,
            "tools": tools,
            "messages": messages,
            # Thinking, effort and prompt caching where the provider has them (#96).
            **request_options(provider, model_name, cfg.effort),
        }
        try:
            response = model.create(request)
        except ModelError as exc:
            raise RunnerError(str(exc), retry=exc.retry) from exc
        counts = pricing.tokens(response.get("usage"))
        for key, value in counts.items():
            totals[key] += value
        cost += 0.0 if provider == "local" else pricing.cost_usd(counts, model_name)
        turns += 1
        stop = response.get("stop_reason")
        _save(
            rt,
            row["id"],
            turns=turns,
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_write_tokens=totals["cache_write"],
            cache_read_tokens=totals["cache_read"],
            cost_usd=round(cost, 6),
            stop_reason=stop,
        )
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        if stop == "refusal":
            raise RunnerError("the model declined this run", retry=False)
        if stop == "pause_turn":
            continue
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if stop != "tool_use" or not uses:
            break
        if totals["output"] >= cfg.max_output_tokens:
            stop = "max_output_tokens"
            break
        results = []
        for block in uses:
            text, is_error = box.call(block.get("name", ""), block.get("input"))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": text,
                    **({"is_error": True} if is_error else {}),
                }
            )
        messages.append({"role": "user", "content": results})
    else:
        stop = "max_turns"
    log.debug("coach runner %s tools used: %s", job, ", ".join(box.calls))
    result = RunResult(status="done", run_id=row["id"], cost_usd=cost, turns=turns, stop=stop)
    finished = utc_now()
    if job in REPORT_JOBS:
        entry = find_entry(rt, job, for_date, started, row["run_key"])
        if entry is None:
            result.status = "budget" if stop == "monthly_cap" else "failed"
            result.error = f"no coach entry written (stopped: {stop})"
            if box.crashed:
                result.error += f"; tools that crashed: {', '.join(box.crashed)}"
            _save(rt, row["id"], status=result.status, error=result.error, finished_at=finished)
            return result
        result.entry_id = entry["id"]
        issues = contract_issues(job, entry, for_date, harness.get("previous"))
        _save(rt, row["id"], harness={**harness, "contract": issues})
        if issues:
            # Fail closed: an entry outside the contract is no report. It is marked
            # invalid, not mailed, and the athlete hears that the run failed.
            for issue in issues:
                log.warning("coach runner %s output contract: %s", job, issue)
            _mark_invalid(rt, entry["id"], issues)
            result.status = "failed"
            result.error = "output contract: " + "; ".join(issues)
            _save(
                rt,
                row["id"],
                status="failed",
                error=result.error[:500],
                entry_id=entry["id"],
                finished_at=finished,
            )
            return result
        coach_replies.mark_read(rt.engine, [r["id"] for r in replies], row["id"])
        _save(rt, row["id"], status="done", entry_id=entry["id"], finished_at=finished)
        if advice_target is not None and not _real_change(rt, advice_target, entry):
            # A new version that only reports or rewords the advice: no push or mail (#90,
            # #147), but the day's notice now points at this version, never at a replaced
            # one (#115).
            _save(rt, row["id"], harness={**harness, "contract": issues, "mail": "no_change"})
            try:
                report.send(rt, job, for_date, entry, advice_day=advice_target, quiet=True)
            except Exception:
                log.exception("coach runner: pointing the notice at the new version failed")
            return result
        _mail(rt, row, job, for_date, entry, result, advice_day=advice_target)
        return result
    _save(rt, row["id"], status="done", finished_at=finished)
    return result


def _advice_target(job: str, for_date: date) -> date | None:
    """The day a daily advice run is for (the evening run: tomorrow); None otherwise."""
    from vibesynergy.query import daily_advice

    return daily_advice.target_day(job, for_date) if job in daily_advice.ADVICE_JOBS else None


def _real_change(rt, target: date, entry: dict[str, Any]) -> bool:
    """The new version is worth a push or a mail (``advice_view.notable``, #147): the first
    advice for a day, another colour, a stricter safety level, or a session decision the
    athlete acts on. A session that is now done, or wording alone, is quiet."""
    from vibesynergy.query import daily_advice

    try:
        return daily_advice.real_change(rt, target, entry)
    except Exception:
        log.exception("coach runner: comparing the advice versions failed; mailing")
        return True


def _mail(
    rt, row, job, for_date, entry, result: RunResult, *, advice_day: date | None = None
) -> None:
    current = get_run(rt, row["job"], row["run_key"]) or {}
    if current.get("mailed_at"):
        result.mailed = True
        return
    try:
        used = report.send(rt, job, for_date, entry, advice_day=advice_day)
    except Exception as exc:  # the run itself is done; the mail can be resent by hand
        log.exception("coach runner: mailing the %s report failed", job)
        _save(rt, row["id"], mail_error=type(exc).__name__)
        return
    if used:
        result.mailed = True
        _save(rt, row["id"], mailed_at=utc_now(), mail_error=None)
    elif contact.is_held(rt, int(entry["id"])):
        # Outside the contact window (#119): it goes out when the window opens.
        _save(rt, row["id"], mail_error=contact.HELD)
    else:
        _save(rt, row["id"], mail_error="not sent: no channel set up (Settings → Alerts)")


def last_runs(rt) -> dict[str, dict[str, Any] | None]:
    """The newest run per job, for the settings page."""
    from vibesynergy.coach_runner import JOBS

    out: dict[str, dict[str, Any] | None] = {}
    with rt.engine.connect() as conn:
        for job in JOBS:
            row = (
                conn.execute(
                    select(coach_run)
                    .where(coach_run.c.job == job)
                    .order_by(coach_run.c.started_at.desc(), coach_run.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            out[job] = (
                {
                    "status": row["status"],
                    "for_date": row["for_date"].isoformat(),
                    "started_at": row["started_at"].isoformat(),
                    "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
                    "turns": row["turns"],
                    "cost_usd": round(row["cost_usd"], 4),
                    "error": row["error"],
                    "harness": row["harness"],
                    "mailed": row["mailed_at"] is not None,
                    "mail_error": row["mail_error"],
                    "entry_id": row["entry_id"],
                }
                if row
                else None
            )
    return out
