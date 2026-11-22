"""Roles per MCP connection, and what each role may write (#118).

More than one AI coach can work under the same harness. Rights hold per **role**, never per
provider or model, and the server enforces them for every write tool:

- ``own``: the athlete's own agent, an interactive session acting on his request. Its
  writes count as approved.
- ``primary``: the one AI coach that gives the daily advice. A plan change needs an
  approval reference (an approved proposal or the athlete's 'akkoord' reply); without one
  it becomes a proposal. Only filling the horizon (a new session in a free slot of a week
  with its approved note) has a standing approval.
- ``advisory``: an AI coach that gives a second opinion. Notes and proposals only: its
  plan changes always become proposals, its daily entries are shown as a second opinion and
  are never the daily advice.

There is exactly one primary. The AI coach inside VibeSynergy (``coach-runner``) is the
primary unless the athlete gives that role to another connection. A connection without a
stored role is ``advisory``: the least it can be, until the athlete chooses in Settings →
Sharing → Coach apps. A local caller (the portal, the CLI, stdio) is the athlete himself.

A write outside the role is refused with a message the model can act on, the same way the
output contract refuses an entry (``coach_runner.contract``, #88).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.identity import LOCAL_CLIENTS
from vibesynergy.store.schema import audit_log, coach_role

OWN = "own"
PRIMARY = "primary"
ADVISORY = "advisory"
ROLES: tuple[str, ...] = (OWN, PRIMARY, ADVISORY)
RUNNER_CLIENT = "coach-runner"
DEFAULT_ROLE = ADVISORY

# Plain words for Settings. English is the catalogue key.
ROLE_LABELS: dict[str, str] = {
    OWN: "My own agent",
    PRIMARY: "Primary AI coach",
    ADVISORY: "Advisory AI coach",
}
# Said to the model, in refusals and in the handbook.
ROLE_NAMES: dict[str, str] = {
    OWN: "the athlete's own agent",
    PRIMARY: "primary AI coach",
    ADVISORY: "advisory AI coach",
}
ROLE_MAY: dict[str, str] = {
    OWN: (
        "You act on the athlete's own request in an interactive session: your writes count "
        "as approved."
    ),
    PRIMARY: (
        "You write the daily advice. A plan change needs approval_id (an approved proposal, "
        "'proposal:<id>', or the athlete's 'akkoord' reply, 'reply:<id>'); without one it "
        "is stored as a proposal the athlete approves under Waiting for you. One standing "
        "approval: adding a new session (vibesynergy_plan_session_set without session_id) in "
        "a slot that held none, in a plan week that has its note, is written directly; keep "
        "it within that week's note and targets."
    ),
    ADVISORY: (
        "You give a second opinion: notes, assessments and proposals. Your morning and "
        "evening entries are shown as a second opinion beside the primary AI coach's "
        "advice, never as the daily advice. Your plan changes (vibesynergy_plan_session_set, "
        "_move, _skip, vibesynergy_plan_week_set, vibesynergy_plan_target_set) and race or "
        "event changes are stored as proposals the athlete approves. You cannot write to "
        "intervals.icu, publish, change the knowledge base directly, or write the AI coach's "
        "memory."
    ),
}

# How a write tool treats each role. Every tool that is not read-only is listed here
# (tests/test_coach_roles.py checks it), so a new write tool cannot skip the question.
OPEN = "open"  # every role, within its scopes
ENTRY = "entry"  # vibesynergy_coach_write: an advisory entry is a second opinion
PLAN = "plan"  # own: direct. primary: approval reference, else a proposal. advisory: proposal
APPROVAL = "approval"  # own: direct. primary: approval reference required. advisory: refused
TRUSTED = "trusted"  # own and primary; advisory refused

WRITE_POLICY: dict[str, str] = {
    # The athlete's diary and activity labels: what he said or confirmed.
    "vibesynergy_add_note": OPEN,
    "vibesynergy_edit_note": OPEN,
    "vibesynergy_checkin": OPEN,
    "vibesynergy_sleep_check_set": OPEN,
    "vibesynergy_log_injury": OPEN,
    "vibesynergy_activity_set_effort": OPEN,
    "vibesynergy_activity_set_coaching": OPEN,
    "vibesynergy_activity_set_purpose": OPEN,
    # Notes and proposals: every AI coach.
    "vibesynergy_coach_write": ENTRY,
    "vibesynergy_coach_log": OPEN,
    "vibesynergy_coach_threshold_propose": OPEN,
    "vibesynergy_kb_propose_change": OPEN,
    # Rebuilds planned sessions after a workout-method change (#142): each one goes through
    # the plan gate as a vibesynergy_plan_session_set, so a proposal unless approved.
    "vibesynergy_workout_rerender": OPEN,
    "vibesynergy_model_run_write": OPEN,
    "vibesynergy_refresh": OPEN,
    # The plan in VibeSynergy.
    "vibesynergy_plan_session_set": PLAN,
    "vibesynergy_plan_session_move": PLAN,
    "vibesynergy_plan_session_skip": PLAN,
    "vibesynergy_plan_week_set": PLAN,
    "vibesynergy_plan_target_set": PLAN,
    # Races and events (#91): the athlete's own agent writes at once; an AI coach's own
    # race change is a proposal (or carries the athlete's approval_id).
    "vibesynergy_race_add": PLAN,
    "vibesynergy_event_add": PLAN,
    "vibesynergy_race_update": PLAN,
    "vibesynergy_race_delete": PLAN,
    "vibesynergy_race_revert": PLAN,
    # Writes outside VibeSynergy's plan: intervals.icu itself.
    "vibesynergy_icu_create_event": APPROVAL,
    "vibesynergy_icu_update_event": APPROVAL,
    "vibesynergy_icu_move_event": APPROVAL,
    "vibesynergy_icu_delete_event": APPROVAL,
    "vibesynergy_icu_revert": APPROVAL,
    # Direct changes an advisory AI coach suggests instead.
    "vibesynergy_race_set_priority": TRUSTED,
    "vibesynergy_coach_memory_write": TRUSTED,
    "vibesynergy_kb_upsert": TRUSTED,
    "vibesynergy_harness_run": TRUSTED,
    "vibesynergy_activity_publish": TRUSTED,
    "vibesynergy_activity_unpublish": TRUSTED,
}

# What an advisory AI coach does instead, per refused tool.
_INSTEAD: dict[str, str] = {
    "vibesynergy_icu_create_event": "propose the change in VibeSynergy's plan with "
    "vibesynergy_plan_session_set; it is stored as a proposal the athlete approves",
    "vibesynergy_icu_update_event": "propose the change in VibeSynergy's plan with "
    "vibesynergy_plan_session_set; it is stored as a proposal the athlete approves",
    "vibesynergy_icu_move_event": "propose the move with vibesynergy_plan_session_move; it is "
    "stored as a proposal the athlete approves",
    "vibesynergy_icu_delete_event": "propose it with vibesynergy_plan_session_skip; it is "
    "stored as a proposal the athlete approves",
    "vibesynergy_icu_revert": "say what should be undone in a note (vibesynergy_coach_write, "
    "entry_type note)",
    "vibesynergy_race_set_priority": "suggest the priority in a note (vibesynergy_coach_write, "
    "entry_type note)",
    "vibesynergy_coach_memory_write": "write what you noticed as a note "
    "(vibesynergy_coach_write, entry_type note); the primary AI coach keeps the memory",
    "vibesynergy_kb_upsert": "suggest the change with vibesynergy_kb_propose_change; the "
    "athlete approves it",
    "vibesynergy_harness_run": "the runs of the primary AI coach are started by the athlete "
    "or the schedule",
    "vibesynergy_activity_publish": "an advisory AI coach does not publish; write your view "
    "as an activity_assessment without public_insight",
    "vibesynergy_activity_unpublish": "an advisory AI coach does not publish; write your view "
    "as a note",
}


class RoleError(ValueError):
    """The write is outside the connection's role. The message is written for the model."""


def refusal(tool: str, role: str, what: str) -> str:
    return f"Refused by this connection's role ({ROLE_NAMES.get(role, role)}): {tool} {what}"


def check_tool(tool: str, role: str) -> None:
    """Refuse a tool a role may never call. Tools that depend on an approval reference
    (``PLAN``, ``APPROVAL``) decide in their handler (``query.plan_proposals.gate``)."""
    policy = WRITE_POLICY.get(tool)
    if role != ADVISORY or policy not in (TRUSTED, APPROVAL):
        return
    instead = _INSTEAD.get(tool, "write a note or a proposal instead")
    raise RoleError(
        refusal(
            tool,
            role,
            f"is not open to an advisory AI coach, which writes notes and proposals only. "
            f"Instead: {instead}.",
        )
    )


# --- the stored roles --------------------------------------------------------------------


def is_local(client: str, scopes: Any) -> bool:
    """A caller without a token (the portal, the CLI, stdio, a test): the athlete himself."""
    return scopes is None or client in LOCAL_CLIENTS


def stored(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(select(coach_role.c.client_id, coach_role.c.role)).all()
    return {r.client_id: r.role for r in rows if r.role in ROLES}


def primary_client(engine: Engine) -> str:
    """The connection that is the primary AI coach: the stored one, else the AI coach
    inside VibeSynergy."""
    found = stored(engine)
    for client, role in found.items():
        if role == PRIMARY and client != RUNNER_CLIENT:
            return client
    return RUNNER_CLIENT


def role_of(engine: Engine, client: str, found: dict[str, str] | None = None) -> str:
    """The role of one connection id (``oa-…``, ``pat:<id>``, ``coach-runner``)."""
    found = stored(engine) if found is None else found
    other_primary = any(r == PRIMARY and c != RUNNER_CLIENT for c, r in found.items())
    if client == RUNNER_CLIENT:
        return ADVISORY if other_primary else PRIMARY
    return found.get(client, DEFAULT_ROLE)


def primary_author(engine: Engine) -> str:
    return f"client:{primary_client(engine)}"


def author_role(engine: Engine, author: str | None, found: dict[str, str] | None = None) -> str:
    """The role behind an entry's author today. ``user`` and the old authors are the
    athlete's own."""
    if not author or not author.startswith("client:"):
        return OWN
    return role_of(engine, author.removeprefix("client:"), found)


def set_role(engine: Engine, client: str, role: str) -> dict[str, str]:
    """Store one connection's role. There is exactly one primary: giving the role to a
    connection takes it from the one that had it, which becomes advisory; taking it away
    gives it back to the AI coach inside VibeSynergy."""
    client = client.strip()
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if not client:
        raise ValueError("a connection id is required")
    if client == RUNNER_CLIENT and role == OWN:
        raise ValueError("The AI coach inside VibeSynergy is a primary or an advisory AI coach.")
    now = utc_now()
    with engine.begin() as conn:
        before = {
            r.client_id: r.role
            for r in conn.execute(select(coach_role.c.client_id, coach_role.c.role))
        }

        def put(target: str, value: str) -> None:
            if target in before:
                conn.execute(
                    update(coach_role)
                    .where(coach_role.c.client_id == target)
                    .values(role=value, updated_at=now)
                )
            else:
                conn.execute(
                    coach_role.insert().values(client_id=target, role=value, updated_at=now)
                )
            before[target] = value

        if role == PRIMARY:
            for other, value in list(before.items()):
                if value == PRIMARY and other != client:
                    put(other, ADVISORY)
            if client != RUNNER_CLIENT:
                put(RUNNER_CLIENT, ADVISORY)
        put(client, role)
        if not any(value == PRIMARY for value in before.values()):
            put(RUNNER_CLIENT, PRIMARY)  # never without a primary
        conn.execute(
            audit_log.insert().values(
                at=now,
                actor="portal",
                action="coach.role.set",
                client_id=client,
                detail={"client_id": client, "role": role},
            )
        )
    return stored(engine)


def current(engine: Engine) -> str:
    """The role of the connection this call runs under."""
    from vibesynergy.mcp import identity

    forced = identity.current_role()
    if forced in ROLES:
        return forced
    client = identity.current_connection().client
    if is_local(client, identity.raw_scopes()):
        return OWN
    return role_of(engine, client)


def key_suffix(author: str) -> str:
    """The author part of a per-author idempotency key: ``morning-<date>-<suffix>``."""
    text = author.removeprefix("client:")
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text).strip("-") or "user"
