"""The current daily advice: one source for every view (#89).

The dashboard, the coach page, ``vibesynergy_today``, the MCP and the mail all read the
advice for a day from ``resolve`` (or ``current``, which computes the rules first). They
can no longer differ.

The setting "Daily advice" (``prefs["daily_advice"]``):

- ``ai`` (default): the newest **valid** AI advice for the day decides everything: the
  colour, the safety level, the reasons and the text. Valid means it carries a safety
  outcome that passes the output contract (``contract.check_safety``) and the runner did
  not mark it invalid. Without one, the rules' advice is shown with a reason from the
  fixed list ``REASONS``.
- ``rules``: the fixed rules only, no AI.
- ``off``: no daily advice.

One exception to "everything from the AI" stays fixed: the autonomous coach runner can
never soften the hard rules (illness, a red-flag note, pain 7/10 or more, an injury that
changes how you move; ``safety_floor.HARD_RULES``). That is enforced where the runner
writes (``contract.check_safety``); a write over MCP is the athlete's own and exempt.

A hard rule that fires after the AI wrote its advice is a new signal: a new AI run is
queued at once (``schedule.enqueue_advice``), the AI advice stays on the page marked as
updating, and the rules take over (reason ``new_signal``) only when that run fails or
cannot run.

Shadow: the rules keep computing beside the AI. Where the two differ (colour or safety
level) it is in ``differences`` for the page (under "Why this advice") and logged once per
day, entry and rules outcome (``audit_log`` action ``advice.shadow``), so it shows over time whether
the AI is too lenient and whether the fallback is any good.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, insert, select

from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.i18n import ENGLISH, Translator
from vibesynergy.query import safety_floor
from vibesynergy.store.schema import audit_log, coach_decision_log, coach_run

log = logging.getLogger(__name__)

MODES = ("ai", "rules", "off")
DEFAULT_MODE = "ai"
PREF = "daily_advice"

# The coach tasks whose entry is a daily advice (their contract carries ``safety``).
ADVICE_JOBS = ("morning", "evening", "injury", "checkin")
ADVICE_ENTRY_TYPES = ("morning", "evening", "note")
# The task a re-run (manual refresh, a new hard signal) uses: the day's advice.
RERUN_JOB = "morning"

# The fixed list of reasons the rules take over, in the athlete's words.
REASONS = {
    "no_run": "No advice from your AI coach yet today: the fixed rules give it.",
    "run_failed": "Your AI coach's run failed: the fixed rules give today's advice.",
    "contract": "Your AI coach's advice was incomplete: the fixed rules give it.",
    "ai_off": "The AI coach is off: the fixed rules give today's advice.",
    "unavailable": "AI budget used up or AI service unavailable: the fixed rules give "
    "today's advice.",
    "new_signal": "Something new came in after your AI coach's advice (pain, illness or a "
    "complaint): the fixed rules give it.",
    "limit": "New data came in and your AI coach reached today's maximum of updates: the "
    "fixed rules give the advice.",
}
UPDATING = {
    "new_signal": "Something new came in (pain, illness or a complaint); your AI coach is "
    "updating this advice.",
    "running": "New data came in; your AI coach is updating this advice.",
    "stale": "New data came in since this advice was written.",
    "waiting": "New data came in; your AI coach updates this advice in a few minutes.",
}
HEADLINES = {
    "green": "Well recovered",
    "amber": "Take it a bit easier",
    "red": "Rest or keep it very easy",
}
COLOUR_NAMES = {"green": "Green", "amber": "Amber", "red": "Red"}
_PROVIDER = re.compile(
    r"could not reach|rate limited|API error|API key|overloaded|local server|monthly_cap",
    re.IGNORECASE,
)


# --- the setting ---------------------------------------------------------------------------


def mode(rt) -> str:
    """The athlete's choice; ``ai`` when unset or unreadable."""
    from vibesynergy.portal import prefs

    data_dir = getattr(getattr(rt, "settings", None), "data_dir", None)
    if data_dir is None:
        return DEFAULT_MODE
    chosen = prefs.load(data_dir).get(PREF)
    return chosen if chosen in MODES else DEFAULT_MODE


# --- validity -------------------------------------------------------------------------------


def ai_safety(meta: dict | None) -> dict[str, Any] | None:
    """The AI's own safety outcome on an entry, when it is valid; else ``None``.

    Checked again on read, so an entry that slipped past (an older harness, a hand-edited
    row) never counts: "AI must always be right"."""
    from vibesynergy.coach_runner import contract

    meta = meta or {}
    if (meta.get("harness") or {}).get("valid") is False:
        return None
    try:
        return contract.check_safety(meta.get("safety"), None)
    except contract.ContractError:
        return None


def owns_safety(rt, meta: dict | None) -> bool:
    """The AI decides this entry's safety: AI mode and a valid outcome on it. Views that
    used to put the rules' safety line beside the coach's words leave it out then."""
    return mode(rt) == "ai" and ai_safety(meta) is not None


# --- reading ---------------------------------------------------------------------------------


def entry_target(entry: dict[str, Any]) -> date | None:
    """The day a daily-advice entry is for (#90): ``meta.harness.target`` when the harness
    recorded it, else its ``for_date``, and the day after for an evening entry (the review
    of a day gives the advice for the next)."""
    harness = (entry.get("meta") or {}).get("harness") or {}
    try:
        if harness.get("target"):
            return date.fromisoformat(str(harness["target"]))
    except ValueError:
        pass
    day = entry.get("for_date")
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if day is None:
        return None
    return day + timedelta(days=1) if entry.get("entry_type") == "evening" else day


def _entries(rt, day: date) -> list[dict[str, Any]]:
    """The coach entries that are a daily advice for ``day`` (they carry ``meta.safety``
    or claim a daily-advice task), newest first: every version of that day's advice."""
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(coach_decision_log)
                .where(
                    coach_decision_log.c.subject_id == 1,
                    coach_decision_log.c.for_date.in_((day, day - timedelta(days=1))),
                    coach_decision_log.c.entry_type.in_(ADVICE_ENTRY_TYPES),
                )
                .order_by(desc(coach_decision_log.c.id))
                .limit(80)
            )
            .mappings()
            .all()
        )
    out = []
    for row in rows:
        meta = row["meta"] or {}
        task = (meta.get("harness") or {}).get("task")
        if meta.get("safety") is None and task not in ADVICE_JOBS and row["entry_type"] == "note":
            continue  # a plain note is no daily advice
        if entry_target(dict(row)) != day:
            continue
        out.append(dict(row))
    return out


def target_day(job: str, run_day: date) -> date:
    """The day a daily-advice run is about: the evening run prepares tomorrow, every other
    advice run (morning, a refresh, a pain or check-in reaction, a new signal) its own day.
    A run's failure only ever affects the advice for this day."""
    return run_day + timedelta(days=1) if job == "evening" else run_day


def _runs(rt, day: date) -> list[dict[str, Any]]:
    """The daily-advice runs whose target day is ``day``, newest first."""
    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(coach_run)
            .where(
                coach_run.c.for_date.in_((day, day - timedelta(days=1))),
                coach_run.c.job.in_(ADVICE_JOBS),
            )
            .order_by(desc(coach_run.c.started_at), desc(coach_run.c.id))
        ).mappings()
        return [dict(r) for r in rows if target_day(r["job"], r["for_date"]) == day]


def _pending(rt, day: date, run_key: str | None = None) -> bool:
    """A daily-advice run for ``day`` waits in the job queue (not claimed yet)."""
    from vibesynergy.coach_runner import schedule
    from vibesynergy.store.schema import job as job_table

    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(job_table.c.payload).where(
                job_table.c.kind == schedule.JOB,
                job_table.c.status.in_(("queued", "running")),
            )
        ).all()
    for (payload,) in rows:
        payload = payload or {}
        name = payload.get("job")
        try:
            run_day = date.fromisoformat(str(payload.get("for_date")))
        except ValueError:
            continue
        if name not in ADVICE_JOBS or target_day(name, run_day) != day:
            continue
        if run_key is None or payload.get("run_key") == run_key:
            return True
    return False


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _run_reason(run: dict[str, Any]) -> str:
    if run["status"] == "budget":
        return "unavailable"
    error = str(run.get("error") or "")
    if error.startswith("output contract"):
        return "contract"
    if _PROVIDER.search(error):
        return "unavailable"
    return "run_failed"


def _runner(rt) -> tuple[bool, bool]:
    """(active, over budget) for the coach runner."""
    try:
        from vibesynergy.coach_runner import runner
        from vibesynergy.coach_runner import settings as runner_settings

        cfg = runner_settings.load(rt)
    except Exception:
        return False, False
    if not (cfg.enabled and cfg.api_key_set):
        return False, False
    try:
        return True, runner.over_budget(rt, cfg)
    except Exception:
        return True, False


def _rules_view(rules: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rules:
        return None
    return {
        "level": rules.get("level"),
        "safety_level": rules.get("safety_level"),
        "rules": list(rules.get("rules") or []),
        "headline": rules.get("headline"),
        "advice": rules.get("advice"),
        "reasons": list(rules.get("reasons") or []),
    }


def _text(headline: str, reasons: list[str], advice: str) -> str:
    shown = [r.rstrip(". ") for r in reasons if r]
    return f"{headline}: {', '.join(shown)}. {advice}" if shown else f"{headline}. {advice}"


def _summary(entry: dict[str, Any]) -> str:
    return (str(entry.get("text") or "").strip().split("\n") or [""])[0].strip()


def _entry_info(entry: dict[str, Any]) -> dict[str, Any]:
    harness = (entry.get("meta") or {}).get("harness") or {}
    at = _aware(entry["at"]) if entry.get("at") else None
    return {
        "id": entry["id"],
        "entry_type": entry["entry_type"],
        "for_date": entry["for_date"].isoformat() if entry.get("for_date") else None,
        "at": at.isoformat() if at else None,
        "via": harness.get("via"),
        "task": harness.get("task"),
        "model": (entry.get("meta") or {}).get("model_id"),
        # Why this version was made (#90): the trigger and what came in before it.
        "trigger": harness.get("trigger"),
        "changes": list(harness.get("changes") or []),
    }


# --- versions (#90) ----------------------------------------------------------------------------


def _decisions(entry: dict[str, Any]) -> dict[str, str]:
    sessions = (entry.get("meta") or {}).get("sessions") or []
    out = {}
    for item in sessions:
        if isinstance(item, dict) and item.get("title"):
            out[str(item["title"]).strip().lower()] = str(item.get("decision") or "")
    return out


def compare(older: dict[str, Any] | None, newer: dict[str, Any]) -> list[dict[str, Any]]:
    """What really changed between two versions of a day's advice: the colour, the safety
    level, or a session decision. Wording alone is no change."""
    if older is None:
        return []
    old_safety = ai_safety(older.get("meta")) or {}
    new_safety = ai_safety(newer.get("meta")) or {}
    out: list[dict[str, Any]] = []
    if old_safety.get("colour") != new_safety.get("colour"):
        out.append(
            {"field": "colour", "from": old_safety.get("colour"), "to": new_safety.get("colour")}
        )
    if old_safety.get("level") != new_safety.get("level"):
        out.append(
            {
                "field": "safety_level",
                "from": old_safety.get("level"),
                "to": new_safety.get("level"),
            }
        )
    before, after = _decisions(older), _decisions(newer)
    changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
    if changed:
        out.append({"field": "sessions", "sessions": changed})
    return out


def versions(entries: list[dict[str, Any]], tr: Translator = ENGLISH) -> list[dict[str, Any]]:
    """Every version of a day's advice, newest first, with what really changed against
    the valid version before it. Invalid versions are listed, marked, and never compared."""
    valid = [e for e in entries if ai_safety(e.get("meta")) is not None]
    out = []
    for entry in entries:
        safety = ai_safety(entry.get("meta"))
        older = next((v for v in valid if v["id"] < entry["id"]), None) if safety else None
        out.append(
            {
                **_entry_info(entry),
                "valid": safety is not None,
                "colour": (safety or {}).get("colour"),
                "safety_level": (safety or {}).get("level"),
                "headline": tr(HEADLINES[safety["colour"]]) if safety else None,
                "advice": _summary(entry),
                "differences": compare(older, entry) if safety else [],
                "first": safety is not None and older is None,
            }
        )
    return out


def real_change(rt, day: date, entry: dict[str, Any]) -> bool:
    """A new version for ``day`` that differs from the previous valid one in colour,
    safety level or a session decision: worth a mail. The first version always is."""
    entries = _entries(rt, day)
    older = next(
        (e for e in entries if e["id"] < entry["id"] and ai_safety(e.get("meta")) is not None),
        None,
    )
    return older is None or bool(compare(older, entry))


def _new_hard(entry: dict[str, Any], rules: dict[str, Any] | None) -> list[str]:
    """Hard rules that fire now and did not when the AI wrote: a new signal."""
    if not rules:
        return []
    now = set(rules.get("rules") or []) & safety_floor.HARD_RULES
    if not now:
        return []
    floor = ((entry.get("meta") or {}).get("harness") or {}).get("rules_floor")
    # Without the rules' outcome at the write, it cannot be shown that the AI saw them.
    seen = set((floor or {}).get("rules") or [])
    return sorted(now - seen)


def _differences(ai: dict[str, Any], rules: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not rules:
        return []
    out = []
    if ai["level"] != rules.get("level"):
        out.append({"field": "colour", "ai": ai["level"], "rules": rules.get("level")})
    if ai["safety_level"] != rules.get("safety_level"):
        out.append(
            {"field": "safety_level", "ai": ai["safety_level"], "rules": rules.get("safety_level")}
        )
    return out


def _shadow_log(rt, day: date, entry_id: int, ai: dict, rules: dict, diffs: list) -> None:
    """One audit row per day, AI entry and rules outcome where the two differ."""
    target = f"{day.isoformat()}:{entry_id}:{rules.get('safety_level')}:" + "+".join(
        sorted(rules.get("rules") or [])
    )
    lenient = None
    if ai.get("safety_level") in safety_floor.LEVELS and rules.get("safety_level") in (
        safety_floor.LEVELS
    ):
        lenient = safety_floor.rank(ai["safety_level"]) < safety_floor.rank(rules["safety_level"])
    try:
        with rt.engine.begin() as conn:
            seen = conn.scalar(
                select(audit_log.c.id)
                .where(audit_log.c.action == "advice.shadow", audit_log.c.target == target)
                .limit(1)
            )
            if seen is not None:
                return
            conn.execute(
                insert(audit_log).values(
                    at=utc_now(),
                    actor="daily_advice",
                    action="advice.shadow",
                    target=target,
                    outcome="lenient" if lenient else "stricter" if lenient is False else "other",
                    detail={
                        "date": day.isoformat(),
                        "entry_id": entry_id,
                        "ai": {"colour": ai.get("level"), "level": ai.get("safety_level")},
                        "rules": {
                            "colour": rules.get("level"),
                            "level": rules.get("safety_level"),
                            "rules": list(rules.get("rules") or []),
                        },
                        "differences": diffs,
                    },
                )
            )
        log.info(
            "daily advice %s: AI %s/%s, rules %s/%s (%s)",
            day,
            ai.get("level"),
            ai.get("safety_level"),
            rules.get("level"),
            rules.get("safety_level"),
            ", ".join(rules.get("rules") or []) or "no rule",
        )
    except Exception:  # the advice must still answer; the log is the shadow
        log.debug("logging the advice difference failed", exc_info=True)


def resolve(
    rt,
    day: date,
    rules: dict[str, Any] | None,
    *,
    tr: Translator = ENGLISH,
    allow_ai: bool = True,
    trigger: bool = True,
) -> dict[str, Any]:
    """The advice for ``day``. ``rules`` is the rules' verdict for that day
    (``dashboard.rules_verdict``), or ``None`` when it could not be computed.

    ``allow_ai`` false shows the rules without the AI's words (a share link that does not
    share the coach). ``trigger`` false never queues a run (tests, a read that must not
    create work)."""
    chosen = mode(rt) if rt is not None else "rules"
    if not allow_ai and chosen == "ai":
        chosen = "rules"
    base: dict[str, Any] = {
        "date": day.isoformat(),
        "mode": chosen,
        "source": None,
        "level": None,
        "safety_level": None,
        "headline": None,
        "advice": None,
        "reasons": [],
        "text": None,
        "fallback": None,
        "updating": None,
        "entry": None,
        "rules": _rules_view(rules),
        "differences": [],
        "override_reason": None,
        "versions": [],
    }
    if chosen == "off":
        base["off_text"] = tr("Daily advice is off. Turn it on in Settings → System.")
        return base
    if chosen == "rules" or rt is None:
        return _from_rules(base, rules, None, tr)

    entries = _entries(rt, day)
    base["versions"] = versions(entries, tr)
    valid = next((e for e in entries if ai_safety(e.get("meta")) is not None), None)
    runs = _runs(rt, day)
    active, over = _runner(rt)
    after = [r for r in runs if valid is None or _aware(r["started_at"]) > _aware(valid["at"])]
    newest = after[0] if after else None

    waiting = _pending(rt, day) and (newest is None or newest["status"] != "running")
    if waiting:
        newest = {"status": "queued", "started_at": utc_now()}
    if valid is None:
        if entries and newest is None:
            reason = "contract"
        elif newest is not None and newest["status"] in ("queued", "running"):
            reason = "no_run"
            base["updating"] = {"kind": "running", "text": tr(UPDATING["running"])}
        elif newest is not None and newest["status"] in ("failed", "budget"):
            reason = _run_reason(newest)
        elif not active:
            reason = "ai_off"
        elif over:
            reason = "unavailable"
        elif entries:
            reason = "contract"
        else:
            reason = "no_run"
        return _from_rules(base, rules, reason, tr)

    # A newer daily-advice run that failed (a refresh, a pain or check-in reaction): the
    # update the day asked for did not come, so the rules take over with its reason.
    fresh = _new_hard(valid, rules)
    if newest is not None and newest["status"] in ("failed", "budget"):
        signal = fresh and newest.get("run_key") == rerun_key(day, fresh)
        return _from_rules(base, rules, "new_signal" if signal else _run_reason(newest), tr)

    safety = ai_safety(valid.get("meta")) or {}
    waiting = _waiting(rt, day) if active and not over else None
    if fresh:
        state = _rerun(rt, day, fresh, active=active, over=over, trigger=trigger)
        if state == "limit":
            return _from_rules(base, rules, "limit", tr)
        if state in ("failed", "unavailable"):
            return _from_rules(base, rules, "new_signal", tr)
        base["updating"] = {"kind": "new_signal", "text": tr(UPDATING["new_signal"])}
    elif newest is not None and newest["status"] in ("queued", "running"):
        base["updating"] = {"kind": "running", "text": tr(UPDATING["running"])}
    elif waiting is not None and waiting["limit"]:
        # New data the AI coach cannot see today: the rules have it (#90).
        return _from_rules(base, rules, "limit", tr)
    elif waiting is not None and not waiting["off"]:
        base["updating"] = {
            "kind": "waiting",
            "text": tr(UPDATING["waiting"]),
            "changes": waiting["changes"],
        }
    elif waiting is not None or _stale(rt, valid):
        base["updating"] = {"kind": "stale", "text": tr(UPDATING["stale"])}

    colour = safety["colour"]
    reasons = list(safety["reasons"])
    advice = _summary(valid)
    headline = tr(HEADLINES[colour])
    base.update(
        source="ai",
        level=colour,
        safety_level=safety["level"],
        headline=headline,
        advice=advice,
        reasons=reasons,
        text=_text(headline, reasons, advice),
        entry={
            **_entry_info(valid),
            # What really changed against the version before it (colour, level, sessions).
            "differences": next(
                (v["differences"] for v in base["versions"] if v["id"] == valid["id"]), []
            ),
        },
        override_reason=safety.get("override_reason"),
    )
    base["differences"] = _differences(base, rules)
    if base["differences"] and rules:
        _shadow_log(rt, day, valid["id"], base, rules, base["differences"])
    return base


def _from_rules(
    base: dict[str, Any], rules: dict[str, Any] | None, reason: str | None, tr: Translator
) -> dict[str, Any]:
    if not rules:
        raise RuntimeError("no advice: the rules could not be computed")
    base.update(
        source="rules",
        level=rules["level"],
        safety_level=rules["safety_level"],
        headline=rules["headline"],
        advice=rules["advice"],
        reasons=list(rules.get("reasons") or []),
        text=rules["text"],
        fallback={"reason": reason, "text": tr(REASONS[reason])} if reason else None,
    )
    return base


def _stale(rt, entry: dict[str, Any]) -> bool:
    """The entry was written before data that changed its inputs (needs_revision)."""
    from vibesynergy.query import coach, coach_workouts

    try:
        snap = coach.snapshot(rt.engine, timezone=rt.settings.timezone)
        found = coach_workouts.coach_entry_freshness(coach_workouts.coach_row(entry), snap, rt)
    except Exception:
        return False
    return found in ("needs_revision", "stale")


def _waiting(rt, day: date) -> dict[str, Any] | None:
    """New data since the newest advice run for ``day`` that no run is making a version
    for yet (``coach_runner.triggers``), unless it waits for the day's fixed run."""
    from vibesynergy.coach_runner import triggers

    found = triggers.pending(rt, day)
    if found is None or found["held"] or found["busy"]:
        return None
    return found


def rerun_key(day: date, rules: list[str]) -> str:
    return f"signal-{day.isoformat()}-{'+'.join(sorted(rules))}"


def _rerun(rt, day: date, fresh: list[str], *, active: bool, over: bool, trigger: bool) -> str:
    """The new AI run for a hard signal: ``queued``, ``running``, ``done``, ``failed`` or
    ``unavailable`` (it cannot run). Queued at most once per day and set of signals."""
    from vibesynergy.coach_runner import runner, schedule, triggers
    from vibesynergy.coach_runner import settings as runner_settings

    key = rerun_key(day, fresh)
    existing = runner.get_run(rt, RERUN_JOB, key)
    if existing is not None:
        # Done, and the signal is still not seen by a valid entry: the update did not come.
        return "running" if existing["status"] == "running" else "failed"
    if _pending(rt, day):
        return "queued"  # this run, or an update for new data (#90) that will see it
    if not active or over:
        return "unavailable"
    try:
        if triggers.limit_reached(rt, runner_settings.load(rt)):
            return "limit"
    except Exception:
        log.debug("reading the advice limit failed", exc_info=True)
    try:
        today = today_in(rt.settings.timezone)
    except Exception:
        today = day
    if day != today:
        return "unavailable"  # the advice task writes for today only
    if trigger and getattr(rt, "jobs", None) is not None:
        try:
            schedule.enqueue_advice(rt, day, key, trigger="signal")
        except Exception:
            log.exception("queueing the advice run for a new signal failed")
            return "failed"
    return "queued"


def current(rt, day: date | None = None, *, tr: Translator = ENGLISH, **kwargs: Any) -> dict:
    """The current advice for ``day`` (default today), computing the rules first. The
    dashboard calls ``resolve`` with the rules it already has; the result is the same."""
    from vibesynergy.query import dashboard

    day = day or today_in(rt.settings.timezone)
    try:
        rules = dashboard.rules_verdict(rt, day, tr=tr)
    except Exception:
        log.exception("the rules' advice could not be computed")
        rules = None
    return resolve(rt, day, rules, tr=tr, **kwargs)


def colour_name(level: str | None, tr: Translator = ENGLISH) -> str:
    return tr(COLOUR_NAMES[level]) if level in COLOUR_NAMES else ""


# --- the manual refresh -----------------------------------------------------------------------


def refresh(rt, *, now: datetime | None = None) -> dict[str, Any]:
    """The dashboard's "Update advice": sync every source (today), then one AI advice run
    that waits for those syncs. Without the AI (setting, runner off) only the syncs run.

    Returns ``{"key", "sync": {source: job id}, "ai": bool, "reason"}``; the page follows
    it with ``refresh_status``."""
    from vibesynergy.coach_runner import schedule
    from vibesynergy.core.timeutil import Window

    now = now or utc_now()
    day = today_in(rt.settings.timezone)
    span = Window(day, day)
    sync: dict[str, int | None] = {}
    for sid in rt.registry.enabled_ids():
        payload = {"source": sid, "dataset": None, "window": str(span)}
        sync[sid] = rt.jobs.enqueue(
            "sync", payload, priority=10, dedupe_key=f"sync:{sid}:None:{span}"
        )
    key = f"refresh-{now.strftime('%Y%m%dT%H%M%S')}"
    reason = None
    chosen = mode(rt)
    active, over = _runner(rt)
    if chosen != "ai":
        reason = "rules" if chosen == "rules" else "off"
    elif not active:
        reason = "ai_off"
    elif over:
        reason = "unavailable"
    ai = reason is None
    if ai:
        ids = [i for i in sync.values() if i]
        schedule.enqueue_advice(rt, day, key, trigger="manual", wait_for=ids, now=now)
    return {"key": key, "date": day.isoformat(), "sync": sync, "ai": ai, "reason": reason}


def refresh_status(rt, key: str, sync_ids: list[int]) -> dict[str, Any]:
    """Where a refresh is: the syncs (done of total, failed sources) and the AI run
    (``waiting``, ``running``, ``done``, ``failed``, ``budget`` or ``None`` without AI)."""
    from vibesynergy.coach_runner import runner, schedule
    from vibesynergy.store.schema import job as job_table

    if not re.fullmatch(r"refresh-\d{8}T\d{6}", key or ""):
        raise ValueError("unknown refresh")
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(job_table.c.id, job_table.c.status, job_table.c.payload).where(
                    job_table.c.id.in_([int(i) for i in sync_ids][:20])
                )
            ).all()
            if sync_ids
            else []
        )
        queued = conn.execute(
            select(job_table.c.status).where(
                job_table.c.dedupe_key == schedule.dedupe_key(RERUN_JOB, key)
            )
        ).first()
    busy = [r for r in rows if r.status in ("queued", "running")]
    failed = [(r.payload or {}).get("source") for r in rows if r.status == "dead"]
    found = runner.get_run(rt, RERUN_JOB, key)
    if found is not None:
        run = found["status"]
    elif queued is not None:
        run = "failed" if queued.status == "dead" else "waiting"
    else:
        run = None
    return {
        "key": key,
        "sync": {"total": len(rows), "done": len(rows) - len(busy), "failed": failed},
        "run": run,
        "finished": not busy and run not in ("waiting", "running"),
    }
