"""MCP tools: races and events (#91).

VibeSynergy's own races and the other events that matter to the plan. Every write needs a
reason, goes through one validation and dedupe (``query.race_events``) and the plan gate
(``query.plan_proposals``): the athlete's own agent writes at once, an AI coach's own race
change becomes a proposal unless it carries the athlete's approval_id. intervals.icu gets a
copy within minutes; Garmin is never written.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.identity import current_author, current_label, current_scopes
from vibesynergy.mcp.server import READ, WRITE, ToolKit, _date

Sport = Literal["run", "ride", "swim", "triathlon", "duathlon", "trail", "other"]
EventKind = Literal["group_training", "camp", "holiday", "unavailable", "other"]


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    tool = kit.tool
    today = kit.today
    envelope = kit.envelope

    def _write(
        name: str, arguments: dict[str, Any], reason: str, approval_id: str | None
    ) -> dict[str, Any]:
        """Through the plan gate, as the plan tools: written when approved for this
        connection's role, else stored as a proposal the athlete approves."""
        from mcp.server.mcpserver.exceptions import ToolError

        from vibesynergy.mcp.scopes import scope_denied
        from vibesynergy.query import plan_model, plan_proposals

        author = current_author() or "mcp"
        held = plan_proposals.gate(
            engine,
            name,
            arguments,
            reason=reason,
            approval_id=approval_id,
            author=author,
            author_name=current_label(),
        )
        if held is not None:
            return envelope(held)
        if "plan.write" not in current_scopes():
            raise ToolError(scope_denied("plan.write"))
        out = plan_proposals.execute(engine, name, arguments, author=author, reason=reason)
        plan_model.mirror_soon(rt.jobs)
        return envelope({**out, "written": True})

    @tool(READ, "read")
    def vibesynergy_races(
        start: str | None = None,
        end: str | None = None,
        kind: Literal["race", "event"] | None = None,
    ) -> dict[str, Any]:
        """VibeSynergy's races and events (group training, camp, holiday, unavailable,
        other) with every field and their id (for vibesynergy_race_update and _delete), the
        copies in Garmin or intervals.icu they are linked to, and the latest changes with
        the audit_id that vibesynergy_race_revert undoes. Default: today and on (start and
        end as 2027-04-11). kind narrows to races or events."""
        first = _date(start) if start else today()
        last = _date(end) if end else None
        from vibesynergy.query import race_events

        return envelope(
            {
                "items": race_events.listing(engine, since=first, until=last, kind=kind),
                "changes": race_events.history(engine),
            },
            span=Window(first, last or first + timedelta(days=365)),
        )

    @tool(WRITE, "plan.write")
    def vibesynergy_race_add(
        reason: str,
        name: str,
        date: str,
        sport: Sport,
        distance_m: float | None = None,
        priority: Literal["A", "B", "C"] | None = None,
        start_time: str | None = None,
        location: str | None = None,
        url: str | None = None,
        goal_time: str | None = None,
        notes: str | None = None,
        end_date: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a race to VibeSynergy, the only source of races. date as 2027-04-11, distance
        in metres, start_time 09:30, goal_time 3:30:00 (H:MM:SS), url https://...; end_date
        only for a race over more days. A race already there on that date with that name
        and sport is refused (change that one); a Garmin or intervals.icu event that is the
        same race becomes its linked copy, never a second race. The next A race is the
        primary race. intervals.icu gets a copy within minutes (RACE_A/B/C, NOTE without a
        priority); Garmin is never written. A reason is required. approval_id: the athlete's
        approval (vibesynergy_coach_handbook, connection); without it an AI coach's own race
        change is stored as a proposal (written: false)."""
        return _write(
            "vibesynergy_race_add",
            {
                "name": name,
                "date": date,
                "sport": sport,
                "distance_m": distance_m,
                "priority": priority,
                "start_time": start_time,
                "location": location,
                "url": url,
                "goal_time": goal_time,
                "notes": notes,
                "end_date": end_date,
            },
            reason,
            approval_id,
        )

    @tool(WRITE, "plan.write")
    def vibesynergy_event_add(
        reason: str,
        kind: EventKind,
        name: str,
        date: str,
        end_date: str | None = None,
        sport: Sport | None = None,
        start_time: str | None = None,
        location: str | None = None,
        url: str | None = None,
        notes: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Add an event that matters to the plan but is no race: kind group_training, camp,
        holiday, unavailable (a time you cannot train) or other. date and end_date (its last
        day, for more days) as 2027-04-11; sport optional. Same checks, dedupe, copy to
        intervals.icu (as a NOTE) and approval as vibesynergy_race_add. Change or delete it
        with vibesynergy_race_update / _delete. A reason is required."""
        return _write(
            "vibesynergy_event_add",
            {
                "kind": kind,
                "name": name,
                "date": date,
                "end_date": end_date,
                "sport": sport,
                "start_time": start_time,
                "location": location,
                "url": url,
                "notes": notes,
            },
            reason,
            approval_id,
        )

    @tool(WRITE, "plan.write")
    def vibesynergy_race_update(
        reason: str,
        race_id: str,
        name: str | None = None,
        date: str | None = None,
        sport: Sport | None = None,
        distance_m: float | None = None,
        priority: Literal["A", "B", "C", ""] | None = None,
        start_time: str | None = None,
        location: str | None = None,
        url: str | None = None,
        goal_time: str | None = None,
        notes: str | None = None,
        end_date: str | None = None,
        kind: Literal["race", "group_training", "camp", "holiday", "unavailable", "other"]
        | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Change one race or event by its id (vibesynergy_races, vibesynergy_coach_state).
        Only what you pass changes; an empty string clears a text field or the priority.
        Same checks, dedupe and approval as vibesynergy_race_add; the intervals.icu copy
        follows within minutes. A change made in intervals.icu or Garmin never changes it.
        A reason is required; vibesynergy_race_revert undoes it."""
        return _write(
            "vibesynergy_race_update",
            {
                "race_id": race_id,
                "name": name,
                "date": date,
                "sport": sport,
                "distance_m": distance_m,
                "priority": priority,
                "start_time": start_time,
                "location": location,
                "url": url,
                "goal_time": goal_time,
                "notes": notes,
                "end_date": end_date,
                "kind": kind,
            },
            reason,
            approval_id,
        )

    @tool(WRITE, "plan.write")
    def vibesynergy_race_delete(
        reason: str, race_id: str, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Delete one race or event by its id. A reason is required. It stays in the history:
        vibesynergy_race_revert with the audit_id brings it back. Its intervals.icu copy is
        removed within minutes; Garmin is never touched. approval_id as for
        vibesynergy_race_add."""
        return _write("vibesynergy_race_delete", {"race_id": race_id}, reason, approval_id)

    @tool(WRITE, "plan.write")
    def vibesynergy_race_revert(
        reason: str, audit_id: int, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Undo one race or event change by its audit_id (vibesynergy_races changes, or the
        audit_id a write returned): an add is deleted, a delete comes back, a change gets
        its earlier values. Refused when the race changed after it (undo that one first). A
        reason is required; approval_id as for vibesynergy_race_add."""
        return _write("vibesynergy_race_revert", {"audit_id": audit_id}, reason, approval_id)
