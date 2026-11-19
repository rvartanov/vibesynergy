"""The harness's output contract, enforced where the entry is written (#88).

One contract for every AI coach: the API runner inside VibeSynergy and any AI app that
comes in over MCP write through ``vibesynergy_coach_write``, and that tool calls ``check_write``
before anything is stored. An entry that misses the contract is **refused** with a message
the model can act on (fail closed): it is never stored, mailed or shown. A weaker model, or
a client that never read the harness rules, cannot get around it.

What is checked, per entry:

1. ``for_date`` is the day the entry is about: today for a morning entry, today or
   tomorrow for an evening entry, never further than a day off.
2. The summary is there and within the task's word limit (with half as much again as
   slack; the rest belongs in ``details``).
3. A daily advice (morning, evening, injury check, check-in reaction) carries ``safety``:
   ``{"level", "colour", "reasons", "override_reason"?}``. The level is one of the floor's
   four, the colour is the one that belongs to the level, there is at least one reason,
   and a level milder than the rules' own needs ``override_reason``: the judgement the
   AI coach made, in a sentence. The autonomous runner can never soften illness, a red-flag
   note or real pain.

This is a check of form and validity, not of approval. What a connection may write, and
what needs the athlete's approval, follows from its role (``mcp.roles``, #118): the
athlete's own agent, the primary AI coach or an advisory AI coach. The entry records both:
``meta.harness.via`` is ``runner`` or ``mcp`` and ``meta.harness.role`` the role.

Every stored entry also gets ``meta.harness``: the harness, rules, prompt and context
versions it was written under, how it came in (runner or MCP) and the rules' outcome at
that moment, so it shows later whether a write followed the current harness, and where the
AI and the rules differed (#89).
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from datetime import date, timedelta
from typing import Any

from vibesynergy.coach_runner import registry
from vibesynergy.query import safety_floor

# Bump when the contract, the context chain format or the stamp changes.
HARNESS_VERSION = 2
LEVELS = safety_floor.LEVELS

WORD_SLACK = 1.5
MAX_REASONS = 5
REASON_CHARS = 200
SAFETY_FIELDS = ("level", "colour", "reasons", "override_reason")
# Without a task, the entry type decides which contract applies.
TASK_OF_ENTRY = {"morning": "morning", "evening": "evening"}
ENTRY_WORDS = {"note": 150}  # the longest report that is a note (week, month, block, race)
_STAMP = re.compile(r"^h(\d+)\.r(\d+)\.p([\w.-]+)\.c(\d+)$")

SAFETY_HELP = (
    'The schema is safety = {"level": "ok"|"ease"|"flag"|"refuse", "colour": '
    '"green"|"amber"|"red", "reasons": ["…", at least one, short], "override_reason": '
    '"…" (only when your level is milder than the rules\' level in the safety context)}. '
    "Colour follows the level: ok=green, ease=amber, flag=amber, refuse=red."
)


class ContractError(ValueError):
    """The entry misses the output contract. The message is written for the model."""


# The run a write belongs to, set by the runner's tool box around each tool call. An
# outside MCP client has none and says which task and harness it follows in the call.
_RUN: ContextVar[dict[str, Any] | None] = ContextVar("vibesynergy_harness_run", default=None)


def set_run(run: dict[str, Any] | None):
    return _RUN.set(run)


def reset_run(token) -> None:
    _RUN.reset(token)


def current_run() -> dict[str, Any] | None:
    return _RUN.get()


def stamp(prompt_version: Any, rules_version: int, context_version: int) -> str:
    """``h<harness>.r<rules>.p<prompt>.c<context>``: what an AI coach was told and given."""
    return f"h{HARNESS_VERSION}.r{rules_version}.p{prompt_version}.c{context_version}"


def parse_stamp(text: Any) -> dict[str, Any] | None:
    found = _STAMP.match(text.strip()) if isinstance(text, str) else None
    if not found:
        return None
    return {
        "harness": int(found.group(1)),
        "rules": int(found.group(2)),
        "prompt": found.group(3),
        "context": int(found.group(4)),
    }


def task_for(entry_type: str, task: str | None) -> registry.Task | None:
    """The registry task whose contract applies to this write, if any."""
    name = task or TASK_OF_ENTRY.get(entry_type)
    if name is None:
        return None
    found = registry.BY_ID.get(name)
    if found is None:
        raise ContractError(
            f"unknown task {name!r}; the tasks are {', '.join(sorted(registry.BY_ID))}"
        )
    wanted = found.contract.entry_type
    if wanted and wanted != entry_type:
        raise ContractError(f"task {name} writes entry_type {wanted!r}, not {entry_type!r}")
    return found


def word_limit(entry_type: str, task: registry.Task | None) -> int | None:
    if task is not None and task.contract.max_words:
        return task.contract.max_words
    return ENTRY_WORDS.get(entry_type)


def _words(text: str) -> int:
    return len(text.split())


def check_safety(
    safety: Any, floor: dict[str, Any] | None, *, autonomous: bool = False
) -> dict[str, Any]:
    """The validated safety outcome, or a ``ContractError`` naming every problem.

    ``floor`` is the rules' outcome for that day (``{"level", "rules"}``); without it the
    comparison with the rules is skipped, the shape is still checked.

    ``autonomous`` is the coach runner acting on its own (a scheduled or event task): it
    may never soften illness or real pain; the same holds for an AI coach over MCP that is
    not the athlete's own agent (#118). The athlete's own agent acts on his request and is
    checked for validity only: a level milder than the rules' needs its reason, whatever
    the rule.
    """
    if not isinstance(safety, dict):
        raise ContractError("safety is required for a daily advice. " + SAFETY_HELP)
    problems: list[str] = []
    unknown = sorted(set(safety) - set(SAFETY_FIELDS))
    if unknown:
        problems.append(f"unknown safety field(s): {', '.join(unknown)}")
    level = safety.get("level")
    if level not in safety_floor.LEVELS:
        problems.append(f"safety.level must be one of {', '.join(safety_floor.LEVELS)}")
    colour = safety.get("colour")
    if level in safety_floor.LEVELS and colour != safety_floor.COLOURS[level]:
        problems.append(
            f"safety.colour must be {safety_floor.COLOURS[level]!r} for level {level!r} "
            f"(got {colour!r})"
        )
    raw_reasons = safety.get("reasons")
    reasons = (
        [str(r).strip()[:REASON_CHARS] for r in raw_reasons if str(r).strip()]
        if isinstance(raw_reasons, list)
        else []
    )
    if not reasons:
        problems.append(
            "safety.reasons needs at least one short reason: what you weighed and why "
            "this level (also when the level is ok)"
        )
    if len(reasons) > MAX_REASONS:
        problems.append(f"safety.reasons has at most {MAX_REASONS} items")
    override = str(safety.get("override_reason") or "").strip()[:400]
    if floor and level in safety_floor.LEVELS and floor.get("level") in safety_floor.LEVELS:
        hard = sorted(set(floor.get("rules") or []) & safety_floor.HARD_RULES)
        milder = safety_floor.rank(level) < safety_floor.rank(floor["level"])
        if milder and hard and autonomous:
            problems.append(
                f"safety.level {level!r} is milder than the rules' {floor['level']!r}, and "
                f"{', '.join(hard)} can never be softened; use {floor['level']!r}"
            )
        elif milder and not override:
            problems.append(
                f"safety.level {level!r} is milder than the rules' {floor['level']!r} "
                f"({', '.join(floor.get('rules') or []) or 'no rule named'}); either use "
                f"{floor['level']!r} or give safety.override_reason: the judgement you made, "
                "in one sentence"
            )
    if problems:
        raise ContractError("; ".join(problems) + ". " + SAFETY_HELP)
    out: dict[str, Any] = {"level": level, "colour": colour, "reasons": reasons[:MAX_REASONS]}
    if override:
        out["override_reason"] = override
    return out


def check_write(
    *,
    entry_type: str,
    summary: str,
    for_date: date | None,
    today: date,
    task: str | None = None,
    safety: Any = None,
    floor: dict[str, Any] | None = None,
    autonomous: bool = False,
) -> dict[str, Any]:
    """Validate one entry before it is stored. This is a check of form and validity
    (schema, a consistent safety outcome, the target day), never of approval: what needs
    the athlete's yes is the registry's ``approval`` and applies to the autonomous runner.
    ``autonomous`` is true for a write from a runner run (``check_safety``).

    Returns ``{"task", "safety"}``: the task id the contract came from and the cleaned
    safety outcome, each possibly ``None``. Raises ``ContractError`` listing everything
    that is wrong, so one retry can fix it all."""
    found = task_for(entry_type, task)
    problems: list[str] = []
    text = (summary or "").strip()
    if not text:
        problems.append("summary is required")
    limit = word_limit(entry_type, found)
    if limit and _words(text) > limit * WORD_SLACK:
        problems.append(
            f"the summary has {_words(text)} words; the contract is {limit}. Shorten it to the "
            "advice and at most four short lines, and move the rest to details"
        )
    if found is not None and "for_date" in found.contract.required and for_date is None:
        problems.append("for_date is required")
    if for_date is not None and found is not None and found.contract.target:
        allowed = _target_days(found.contract.target, today)
        if for_date not in allowed:
            days = " or ".join(d.isoformat() for d in allowed)
            problems.append(
                f"for_date {for_date.isoformat()} is not the day this {found.id} entry is "
                f"about; it must be {days} (today is {today.isoformat()})"
            )
    cleaned = None
    if (found is not None and found.contract.safety) or safety is not None:
        try:
            cleaned = check_safety(safety, floor, autonomous=autonomous)
        except ContractError as exc:
            problems.append(str(exc))
    if problems:
        raise ContractError("Refused by the output contract: " + " | ".join(problems))
    return {"task": found.id if found else None, "safety": cleaned}


def _target_days(target: str, today: date) -> tuple[date, ...]:
    if target == "today":
        return (today,)
    if target == "today_or_tomorrow":
        return (today, today + timedelta(days=1))
    return (today - timedelta(days=1), today, today + timedelta(days=1))


def advice_target(
    entry_type: str, task: str | None, for_date: date, today: date, has_safety: bool
) -> date | None:
    """The day a daily advice is for (#90), stored as ``meta.harness.target``.

    A runner run says it (``current_run()["target"]``: the evening run's advice is for
    tomorrow). Over MCP: an evening entry written for today is the review of today and its
    advice is for tomorrow; an evening entry for tomorrow is about tomorrow; every other
    daily advice is for its ``for_date``. ``None`` for an entry that is no daily advice."""
    run = current_run()
    if run and run.get("target") and task == run.get("task"):
        return run["target"]
    found = registry.BY_ID.get(task or TASK_OF_ENTRY.get(entry_type, ""))
    if not has_safety and not (found and found.contract.safety):
        return None
    if entry_type == "evening" and for_date <= today:
        return for_date + timedelta(days=1)
    return for_date


def harness_meta(
    *,
    task: str | None,
    declared: Any,
    current: str | None,
    floor: dict[str, Any] | None,
    target: date | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """What is stored on the entry (``meta.harness``): which harness it was written under.

    ``role`` is the writing connection's role at that moment (``mcp.roles``, #118): an
    ``advisory`` entry is a second opinion and never the daily advice.

    ``declared`` is the stamp the writer says it followed (from ``vibesynergy_coach_prompt``;
    the runner's is set by the harness itself). ``follows_current`` is true only when that
    stamp is the current one for the task."""
    run = current_run()
    via = "runner" if run else "mcp"
    if run:
        declared = run.get("stamp")
    parsed = parse_stamp(declared)
    extra: dict[str, Any] = {}
    if role:
        extra["role"] = role
    if target is not None:
        extra["target"] = target.isoformat()
    if run and run.get("target") and task == run.get("task"):
        # Why this version was made: the trigger and what came in since the last one.
        extra["trigger"] = run.get("trigger")
        extra["changes"] = list(run.get("changes") or [])
    return {
        **extra,
        "version": HARNESS_VERSION,
        "via": via,
        "task": task,
        "stamp": declared if parsed else None,
        "current": current,
        "follows_current": bool(parsed and current and declared == current),
        "rules_floor": (
            {"level": floor.get("level"), "rules": list(floor.get("rules") or [])}
            if floor
            else None
        ),
    }
