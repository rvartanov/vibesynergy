"""The coach handbook: the provider-neutral protocol plus everything generated around it.

Any AI that coaches over MCP reads this first (``vibesynergy_coach_handbook``). The protocol
text is ``docs/COACH_PROTOCOL.md`` (packaged into the wheel, so an installed app serves it,
#119). It is the way of working and states no task, time or rule of its own. The rest is
generated, never written by hand, from the same sources the AI coach inside VibeSynergy
runs on: the task register with each task's schedule, triggers, context chain, contract and
approval steps (``coach_tasks``, from ``coach_runner.registry`` and the settings), the
harness versions and contract (``harness_index``), the roles and approval steps
(``approval_index``), the contact window (``coach_runner.contact``) and the knowledge-base
index (active articles and the rule ids to cite in ``kb_rules``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.store.schema import kb_article, kb_rule, kb_source

PROTOCOL_DOC = "docs/COACH_PROTOCOL.md"
# Enough for the whole coach loop; diary.write and sync are optional extras, and publish is
# only needed to set the public insight line of an activity.
COACH_SCOPES_NEEDED: tuple[str, ...] = ("read", "coach.write", "plan.propose", "plan.write")
COACH_SCOPES_OPTIONAL: tuple[str, ...] = ("diary.write", "sync", "publish")
START_HERE: tuple[str, ...] = (
    "vibesynergy_coach_handbook",
    "vibesynergy_sync_status",
    "vibesynergy_coach_state",
    "vibesynergy_kb_get",
)


# Where the wheel carries the protocol (pyproject.toml, ``force-include``, #119): an installed
# app serves the same text as the repository's docs/COACH_PROTOCOL.md.
PACKAGED_DOC = Path(__file__).resolve().parents[1] / "_docs" / "COACH_PROTOCOL.md"


def _protocol_path() -> Path | None:
    """The protocol the wheel carries; in a checkout (an editable install, the tests) the
    repository's own file."""
    if PACKAGED_DOC.is_file():
        return PACKAGED_DOC
    for parent in Path(__file__).resolve().parents:
        candidate = parent / PROTOCOL_DOC
        if candidate.is_file():
            return candidate
    return None


def protocol_text() -> str | None:
    path = _protocol_path()
    return path.read_text(encoding="utf-8") if path else None


# What an article is and who keeps it. Routines (the prompts and the coach loop) are
# the athlete's: changed in the portal or with their approval. Evidence and methods the coach
# keeps up, with source corrections proposed (vibesynergy_kb_propose_change), not applied.
ROUTINE_TOPICS = frozenset({"coach-prompts", "coach-loop"})


def article_kind(slug: str, topic: str) -> tuple[str, str]:
    """(kind, owner) of one article: routine/athlete, evidence/coach or method/coach."""
    if topic in ROUTINE_TOPICS:
        return "routine", "athlete"
    if slug.startswith("evidence-"):
        return "evidence", "coach"
    return "method", "coach"


def knowledge_index(engine: Engine) -> dict[str, Any]:
    """Active articles and the active rules in them, in a stable order."""
    with engine.connect() as conn:
        articles = (
            conn.execute(
                select(kb_article)
                .where(kb_article.c.status == "active")
                .order_by(kb_article.c.topic, kb_article.c.slug)
            )
            .mappings()
            .all()
        )
        slug_of = {row["id"]: row["slug"] for row in articles}
        sources = {
            article_id: (total, verified or 0)
            for article_id, total, verified in conn.execute(
                select(
                    kb_source.c.article_id,
                    func.count(),
                    func.sum(kb_source.c.verified_at.is_not(None)),
                ).group_by(kb_source.c.article_id)
            )
        }
        rules = (
            conn.execute(
                select(kb_rule)
                .where(kb_rule.c.active.is_(True), kb_rule.c.article_id.in_(list(slug_of)))
                .order_by(kb_rule.c.id)
            )
            .mappings()
            .all()
            if slug_of
            else []
        )
    return {
        "articles": [
            {
                "slug": row["slug"],
                "title": row["title"],
                "topic": row["topic"],
                "evidence_grade": row["evidence_grade"],
                "version": row["version"],
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                **dict(
                    zip(("kind", "owner"), article_kind(row["slug"], row["topic"]), strict=True)
                ),
                "sources": {
                    "total": sources.get(row["id"], (0, 0))[0],
                    "verified": sources.get(row["id"], (0, 0))[1],
                },
            }
            for row in articles
        ],
        "rule_ids": [row["id"] for row in rules],
        "rules": [
            {
                "id": row["id"],
                "article": slug_of[row["article_id"]],
                "grade": row["grade"],
                "version": row["version"],
            }
            for row in rules
        ],
    }


def _tasks(rt: Any | None) -> list[dict[str, Any]]:
    """The task register (``coach_tasks``): generated from the harness registry and the
    settings, with last runs and missing runs when the install is known (#119)."""
    from vibesynergy.query import coach_tasks

    if rt is None:
        return coach_tasks.register()
    return coach_tasks.status_for(rt)


def approval_index() -> dict[str, Any]:
    """Who may write what, and what waits for the athlete's yes: from the roles
    (``mcp.roles``), the plan approval check (``plan_proposals``) and the registry."""
    from vibesynergy.coach_runner import registry
    from vibesynergy.mcp import roles
    from vibesynergy.query import plan_proposals

    policies: dict[str, list[str]] = {}
    for name, policy in roles.WRITE_POLICY.items():
        policies.setdefault(policy, []).append(name)
    return {
        "roles": {
            role: {"name": roles.ROLE_NAMES[role], "may": roles.ROLE_MAY[role]}
            for role in roles.ROLES
        },
        "default_role": roles.DEFAULT_ROLE,
        "approval_id": plan_proposals.APPROVAL_HELP,
        "approval_hours": plan_proposals.APPROVAL_HOURS,
        "write_policy": {policy: sorted(names) for policy, names in sorted(policies.items())},
        "write_policy_help": {
            roles.OPEN: "every role, within its scopes",
            roles.ENTRY: "every role; an advisory AI coach's daily entry is a second opinion",
            roles.PLAN: "the athlete's own agent writes; the primary AI coach needs "
            "approval_id, else a proposal is stored; an advisory AI coach always proposes",
            roles.APPROVAL: "the athlete's own agent writes; the primary AI coach needs "
            "approval_id; an advisory AI coach is refused",
            roles.TRUSTED: "the athlete's own agent and the primary AI coach; an advisory "
            "AI coach is refused and told what to do instead",
        },
        "tasks": {task.id: list(task.approval) for task in registry.TASKS if task.approval},
    }


def harness_index() -> dict[str, Any]:
    """The harness every coach works under, over MCP as much as inside VibeSynergy (#88):
    its versions, how to get a task's rules and context, and what a write must meet."""
    from vibesynergy.coach_runner import context, contract, prompts, registry

    return {
        "version": contract.HARNESS_VERSION,
        "rules_version": prompts.RULES_VERSION,
        "context_version": context.VERSION,
        "how": [
            "Before a task: vibesynergy_coach_prompt(job, include_context=true). It returns "
            "the task's prompt (body_md), the harness rules (rules_md), the output contract, "
            "the preloaded context (the safety block with the rules' level and reasons "
            "first) and harness.stamp.",
            "Follow rules_md where it differs from body_md.",
            "Write with vibesynergy_coach_write(..., task=<job>, harness=<harness.stamp>). "
            "The server checks every write against the contract and refuses what misses it; "
            "read the error, fix that, write again.",
            "A day has one advice thread (#115): when the context block day_thread shows a "
            "current version for the day your advice is for, yours is the next version. "
            "Build on it and pass changed_because={previous: <its id>, reason: what changes "
            "and why}; without it the write is refused.",
            "Times are this install's: each task's due and days in tasks (local time, "
            "timezone). Reach the athlete only inside contact_window; outside it write "
            "your entries as usual and send nothing until opens_at, except its "
            "urgent_exception.",
        ],
        "daily_advice_tasks": list(registry.daily()),
        "safety_schema": contract.SAFETY_HELP,
        "changed_because_schema": contract.CHANGED_HELP,
        "tasks": {
            task.id: {
                "entry_type": task.contract.entry_type,
                "max_words": task.contract.max_words,
                "target_day": task.contract.target or None,
                "safety_required": task.contract.safety,
                "chain": list(registry.chain_for(task.id)),
            }
            for task in registry.TASKS
            if task.status == "live" and task.runs == "model"
        },
    }


def _contact(rt: Any | None) -> dict[str, Any] | None:
    from vibesynergy.coach_runner import contact

    return contact.state(rt) if rt is not None else None


def handbook(
    engine: Engine,
    *,
    scopes: frozenset[str] | set[str],
    include_protocol: bool = True,
    rt: Any | None = None,
    connection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """The handbook for one connection, and warnings for the envelope.

    Everything but the protocol text is generated (#119): the tasks with their schedule,
    triggers, context chain and contract, the approval steps and the contact window come
    from the harness registry, the roles and the settings of this install (``rt``), the
    same ones the AI coach inside VibeSynergy runs on."""
    warnings: list[str] = []
    text = protocol_text()
    if text is None:
        warnings.append(f"{PROTOCOL_DOC} is not part of this install; read it in the repository")
    missing = [scope for scope in COACH_SCOPES_NEEDED if scope not in scopes]
    if missing:
        warnings.append("this connection lacks scopes the coach loop needs: " + ", ".join(missing))
    from vibesynergy.query import knowledge

    # An instruction or COACH rule that names a model (#96): said to every AI coach.
    warnings.extend(item["message"] for item in knowledge.lint(engine))
    data: dict[str, Any] = {
        "protocol_version": (
            hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text is not None else None
        ),
        "start_here": list(START_HERE),
        "harness": harness_index(),
        "knowledge": knowledge_index(engine),
        "tasks": _tasks(rt),
        "timezone": rt.settings.timezone if rt is not None else None,
        "contact_window": _contact(rt),
        "approval": approval_index(),
        "access": {
            "scopes": sorted(scopes),
            "needed": list(COACH_SCOPES_NEEDED),
            "optional": list(COACH_SCOPES_OPTIONAL),
            "missing": missing,
        },
    }
    if connection is not None:
        # Who this connection is among the AI coaches, and what that role may write (#118).
        data["connection"] = connection
    if include_protocol:
        data["protocol_md"] = text
    return data, warnings
