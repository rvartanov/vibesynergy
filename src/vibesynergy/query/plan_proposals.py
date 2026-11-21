"""Plan changes that need the athlete's yes: proposals and approval references (#118).

A plan write is carried out at once only when it is approved:

- the athlete's own agent (role ``own``): the request is his, so it is approved;
- the primary AI coach with an **approval reference**: ``proposal:<id>`` (a proposal the
  athlete approved) or ``reply:<id>`` (his 'akkoord' reply to a coach entry), not older
  than ``APPROVAL_HOURS``;
- the primary AI coach **adding** a session in a slot that never held one, in a plan week
  that has its (approved) note: the standing approval for filling the horizon
  (``is_horizon_fill``);
- anything else (the primary without a reference, an advisory AI coach always) is stored
  as a **proposal**: the tool call as it came in. The athlete approves or rejects it under
  Waiting for you; approving carries it out, in the proposer's name.

``gate`` is called by every plan tool before it writes; ``require_approval`` by the
intervals.icu write tools, which have no proposal form.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp import roles
from vibesynergy.store.schema import audit_log, coach_reply, plan_proposal

APPROVAL_HOURS = 48
OPEN, APPROVED, REJECTED = "open", "approved", "rejected"
PLAN_TOOLS = tuple(tool for tool, policy in roles.WRITE_POLICY.items() if policy == roles.PLAN)

# The athlete's yes in a reply, and what turns it into a no.
_YES = re.compile(
    r"\b(akkoord|goedgekeurd|agree|agreed|approve|approved|go ahead|doe maar|is goed|prima|"
    r"ja|yes|ok|oke|oké|okay)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"\b(niet|geen|not|no|nee|don't|do not|never|nooit)\b", re.IGNORECASE)

APPROVAL_HELP = (
    "approval_id is 'proposal:<id>' (a proposal the athlete approved under Waiting for "
    "you) or 'reply:<id>' (the athlete's 'akkoord' reply to a coach entry), not older than "
    f"{APPROVAL_HOURS} hours. Without approval_id the change is stored as a proposal; only "
    "adding a new session in a free slot of a week that has its note needs none."
)


class ApprovalError(roles.RoleError):
    """The approval reference does not hold. The message is written for the model."""


def is_yes(text: str) -> bool:
    """An explicit yes without a no in it: 'akkoord', 'ja, doe maar', 'approved'."""
    return bool(_YES.search(text or "")) and not _NO.search(text or "")


def check_approval(engine: Engine, approval_id: str) -> dict[str, Any]:
    """The approval a reference points at, or an ``ApprovalError`` saying what is wrong."""
    text = (approval_id or "").strip()
    kind, _, ident = text.partition(":")
    if kind not in ("proposal", "reply") or not ident.strip().isdigit():
        raise ApprovalError(
            f"Refused: approval_id {approval_id!r} is not a reference. " + APPROVAL_HELP
        )
    number = int(ident)
    oldest = utc_now() - timedelta(hours=APPROVAL_HOURS)
    with engine.connect() as conn:
        if kind == "proposal":
            row = (
                conn.execute(select(plan_proposal).where(plan_proposal.c.id == number))
                .mappings()
                .first()
            )
            if row is None:
                raise ApprovalError(f"Refused: there is no proposal {number}. " + APPROVAL_HELP)
            if row["status"] != APPROVED:
                raise ApprovalError(
                    f"Refused: proposal {number} is {row['status']}, not approved by the athlete. "
                    "Nothing was changed."
                )
            at = row["decided_at"]
        else:
            row = (
                conn.execute(select(coach_reply).where(coach_reply.c.id == number))
                .mappings()
                .first()
            )
            if row is None:
                raise ApprovalError(f"Refused: there is no reply {number}. " + APPROVAL_HELP)
            if row["author"] != "user" or not is_yes(row["text"]):
                raise ApprovalError(
                    f"Refused: reply {number} is not an explicit 'akkoord' from the athlete. "
                    "Nothing was changed. Without approval_id the change is stored as a "
                    "proposal."
                )
            at = row["updated_at"] or row["at"]
    if at is None or at < oldest:
        raise ApprovalError(
            f"Refused: the approval in {text} is older than {APPROVAL_HOURS} hours. An old "
            "approval is no approval: leave approval_id out and the change is stored as a "
            "new proposal."
        )
    return {"kind": kind, "id": number, "at": at.isoformat()}


def require_approval(engine: Engine, tool: str, approval_id: str | None) -> None:
    """For a write without a proposal form (intervals.icu): the athlete's own agent, or the
    primary AI coach with an approval reference."""
    role = roles.current(engine)
    if role == roles.OWN:
        return
    roles.check_tool(tool, role)
    if not (approval_id or "").strip():
        raise roles.RoleError(
            roles.refusal(
                tool,
                role,
                "needs the athlete's approval and has no proposal form. Make the change in "
                "VibeSynergy's plan (vibesynergy_plan_session_set, _move, _skip): without "
                "approval_id it is stored as a proposal the athlete approves. " + APPROVAL_HELP,
            )
        )
    check_approval(engine, approval_id or "")


# --- carrying a plan change out -----------------------------------------------------------------


def _day(text: Any) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError as exc:
        raise ValueError(f"dates look like 2026-09-24, got {text!r}") from exc


def execute(
    engine: Engine, tool: str, arguments: dict[str, Any], *, author: str, reason: str
) -> dict[str, Any]:
    """Carry out one plan tool call in VibeSynergy's plan. Raises ``ValueError`` with a
    message for whoever asked."""
    from vibesynergy.query import plan_model

    if plan_model.current_plan_id(engine) is None:
        raise ValueError("there is no plan yet: create one with `vibesynergy plan create`")
    a = arguments
    if tool == "vibesynergy_plan_session_set":
        return plan_model.set_session(
            engine,
            session_id=a.get("session_id"),
            day=_day(a.get("date")),
            name=a.get("name") or "",
            sport=a.get("sport"),
            description=a.get("description") or "",
            reason=reason,
            author=author,
            slot=a.get("slot"),
            template=a.get("template"),
        )
    if tool == "vibesynergy_plan_session_move":
        return plan_model.move_session(
            engine,
            session_id=a.get("session_id") or "",
            day=_day(a.get("date")),
            reason=reason,
            author=author,
        )
    if tool == "vibesynergy_plan_session_skip":
        return plan_model.skip_session(
            engine, session_id=a.get("session_id") or "", reason=reason, author=author
        )
    if tool == "vibesynergy_plan_week_set":
        return plan_model.set_week(
            engine, week=int(a["week"]), note=a.get("note") or "", reason=reason, author=author
        )
    if tool == "vibesynergy_plan_target_set":
        return plan_model.set_target(
            engine,
            week=int(a["week"]),
            sport=a.get("sport") or "",
            time_s=a.get("time_s"),
            distance_m=a.get("distance_m"),
            load=a.get("load"),
            reason=reason,
            author=author,
        )
    raise ValueError(f"{tool} is not a plan tool")


def _session(engine: Engine, session_id: str) -> dict[str, Any]:
    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import plan_session

    plan_id = plan_model.current_plan_id(engine)
    if plan_id is None:
        raise ValueError("there is no plan yet: create one with `vibesynergy plan create`")
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(plan_session).where(
                    plan_session.c.plan_id == plan_id, plan_session.c.id == session_id
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError(f"no session {session_id!r} in the plan")
    if row["status"] != "planned":
        raise ValueError(f"session {session_id!r} is {row['status']}, not planned")
    return dict(row)


def describe(engine: Engine, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What a proposal would do, for the athlete: checked as far as it can be without
    writing, so a proposal that can never be carried out is refused when it is made."""
    from vibesynergy.query import garmin_strength, plan_model, workout_model

    if plan_model.current_plan_id(engine) is None:
        raise ValueError("there is no plan yet: create one with `vibesynergy plan create`")
    a = arguments
    if tool == "vibesynergy_plan_session_set":
        day = _day(a.get("date"))
        parsed = workout_model.parse_intervals(a.get("description") or "")
        if parsed.unparsed:
            raise ValueError(f"steps not understood: {parsed.unparsed}")
        garmin_strength.check_description(a.get("description") or "")
        if a.get("session_id"):
            before = _session(engine, a["session_id"])
            return {
                "action": "change",
                "name": a.get("name") or before["name"],
                "date": day.isoformat(),
                "from_date": before["date"].isoformat(),
                "sport": a.get("sport") or before["sport"],
                "text": a.get("description") or "",
            }
        return {
            "action": "add",
            "name": a.get("name") or "",
            "date": day.isoformat(),
            "sport": a.get("sport"),
            "text": a.get("description") or "",
        }
    if tool == "vibesynergy_plan_session_move":
        before = _session(engine, a.get("session_id") or "")
        return {
            "action": "move",
            "name": before["name"],
            "sport": before["sport"],
            "from_date": before["date"].isoformat(),
            "date": _day(a.get("date")).isoformat(),
        }
    if tool == "vibesynergy_plan_session_skip":
        before = _session(engine, a.get("session_id") or "")
        return {
            "action": "skip",
            "name": before["name"],
            "sport": before["sport"],
            "date": before["date"].isoformat(),
        }
    if tool == "vibesynergy_plan_week_set":
        return {"action": "week", "week": int(a["week"]), "text": a.get("note") or ""}
    if tool == "vibesynergy_plan_target_set":
        return {
            "action": "target",
            "week": int(a["week"]),
            "sport": a.get("sport"),
            "time_s": a.get("time_s"),
            "distance_m": a.get("distance_m"),
            "load": a.get("load"),
        }
    raise ValueError(f"{tool} is not a plan tool")


# --- proposals -------------------------------------------------------------------------------


def _row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("created_at", "decided_at"):
        data[key] = data[key].isoformat() if data.get(key) else None
    return data


def propose(
    engine: Engine,
    *,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
    author: str,
    author_name: str | None,
    role: str,
) -> dict[str, Any]:
    """Store one plan tool call as a proposal. The same open proposal by the same author is
    returned, not stored twice (a retry, or a routine that runs again)."""
    if tool not in PLAN_TOOLS:
        raise ValueError(f"{tool} is not a plan tool")
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    arguments = {k: v for k, v in arguments.items() if v is not None}
    summary = describe(engine, tool, arguments)
    with engine.begin() as conn:
        for row in conn.execute(
            select(plan_proposal).where(
                plan_proposal.c.status == OPEN,
                plan_proposal.c.author == author,
                plan_proposal.c.tool == tool,
            )
        ).mappings():
            if row["arguments"] == arguments:
                return _row(row)
        ident = conn.execute(
            plan_proposal.insert()
            .values(
                created_at=utc_now(),
                author=author,
                author_name=(author_name or "").strip() or None,
                role=role,
                tool=tool,
                arguments=arguments,
                reason=reason.strip(),
                summary=summary,
                status=OPEN,
            )
            .returning(plan_proposal.c.id)
        ).scalar_one()
        row = (
            conn.execute(select(plan_proposal).where(plan_proposal.c.id == ident)).mappings().one()
        )
        return _row(row)


def is_horizon_fill(engine: Engine, tool: str, arguments: dict[str, Any]) -> bool:
    """The standing approval (the athlete's decision of 19-11-2026): the primary AI coach
    may **add** a session where there was none, within a week whose note is approved,
    without a separate approval (filling the horizon, the morning run's "day 9").

    True only for ``vibesynergy_plan_session_set`` without ``session_id``, when the slot
    it would fill (that day and slot, or that day and sport) never held a session (a
    skipped one counts: putting it back is a change) and the plan week of that day has a
    note. A week note is written by the athlete or with his approval, so a note that is
    there is approved. Changing, moving or skipping a session, a week note and a target are
    never a fill."""
    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import plan_session, plan_week

    if tool != "vibesynergy_plan_session_set" or arguments.get("session_id"):
        return False
    plan_id = plan_model.current_plan_id(engine)
    if plan_id is None:
        return False
    try:
        day = _day(arguments.get("date"))
    except ValueError:
        return False
    slot = arguments.get("slot") or (arguments.get("sport") or "session").lower()
    ident = f"{plan_id}:{day.isoformat()}:{slot}"  # the id plan_model.set_session gives it
    monday = day - timedelta(days=day.weekday())
    with engine.connect() as conn:
        taken = conn.execute(select(plan_session.c.id).where(plan_session.c.id == ident)).first()
        note = conn.execute(
            select(plan_week.c.note).where(
                plan_week.c.plan_id == plan_id, plan_week.c.monday == monday
            )
        ).scalar()
    return taken is None and bool((note or "").strip())


def gate(
    engine: Engine,
    tool: str,
    arguments: dict[str, Any],
    *,
    reason: str,
    approval_id: str | None,
    author: str,
    author_name: str | None = None,
) -> dict[str, Any] | None:
    """Before a plan tool writes: ``None`` when the write is approved and goes ahead, else
    the stored proposal (nothing changed in the plan). A reference that does not hold is
    refused (``ApprovalError``), never silently turned into a proposal."""
    role = roles.current(engine)
    if role == roles.OWN:
        return None
    if role == roles.PRIMARY and (approval_id or "").strip():
        check_approval(engine, approval_id or "")
        return None
    if role == roles.PRIMARY and is_horizon_fill(engine, tool, arguments):
        return None  # the standing approval: a new session in a free slot of an approved week
    found = propose(
        engine,
        tool=tool,
        arguments=arguments,
        reason=reason,
        author=author,
        author_name=author_name,
        role=role,
    )
    return {
        "proposal": found,
        "written": False,
        "note": (
            f"Stored as proposal {found['id']}: nothing changed in the plan. The athlete "
            "approves or rejects it under Waiting for you; when approved, VibeSynergy carries "
            "it out itself. Do not write it again. Say in your entry that you proposed it."
            + (
                " An advisory AI coach's plan changes are always proposals."
                if role == roles.ADVISORY
                else " " + APPROVAL_HELP
            )
        ),
    }


def pending(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_proposal).where(plan_proposal.c.status == OPEN).order_by(plan_proposal.c.id)
        ).mappings()
        return [_row(r) for r in rows]


def get(engine: Engine, proposal_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(plan_proposal).where(plan_proposal.c.id == proposal_id))
            .mappings()
            .first()
        )
    return _row(row) if row else None


def _decide(conn: Any, proposal_id: int, status: str, result: dict[str, Any] | None) -> None:
    now = utc_now()
    conn.execute(
        update(plan_proposal)
        .where(plan_proposal.c.id == proposal_id)
        .values(status=status, decided_at=now, result=result)
    )
    conn.execute(
        audit_log.insert().values(
            at=now,
            actor="portal",
            action=f"plan.proposal.{status}",
            target=f"plan_proposal:{proposal_id}",
            detail={"id": proposal_id},
        )
    )


def approve(rt: Any, proposal_id: int) -> dict[str, Any]:
    """The athlete's yes: carry the proposal out, in the proposer's name, and mark it
    approved. When it can no longer be carried out (the session is gone, the plan changed)
    it stays open and the error says why."""
    from vibesynergy.query import plan_model

    engine = rt.engine
    found = get(engine, proposal_id)
    if found is None:
        raise ValueError("No proposal with that id.")
    if found["status"] != OPEN:
        raise ValueError("This proposal is already handled.")
    reason = f"{found['reason']} (approved by the athlete, proposal {proposal_id})"
    out = execute(engine, found["tool"], found["arguments"], author=found["author"], reason=reason)
    with engine.begin() as conn:
        _decide(conn, proposal_id, APPROVED, {"version": out.get("version")})
    jobs = getattr(rt, "jobs", None)
    if jobs is not None:
        plan_model.mirror_soon(jobs)
    return get(engine, proposal_id) or found


def reject(engine: Engine, proposal_id: int) -> dict[str, Any]:
    found = get(engine, proposal_id)
    if found is None:
        raise ValueError("No proposal with that id.")
    if found["status"] != OPEN:
        raise ValueError("This proposal is already handled.")
    with engine.begin() as conn:
        _decide(conn, proposal_id, REJECTED, None)
    return get(engine, proposal_id) or found
