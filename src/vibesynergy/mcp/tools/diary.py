"""MCP tools: diary: notes, check-in, injuries (split from mcp/server.py)."""

from __future__ import annotations

from typing import Any

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.identity import note_source
from vibesynergy.mcp.server import (
    DESTRUCTIVE,
    READ,
    WRITE,
    ToolKit,
    _date,
)
from vibesynergy.query import (
    journal,
    views,
)


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    tool = kit.tool
    today = kit.today
    window = kit.window
    envelope = kit.envelope
    diary_changed = kit.diary_changed

    # Diary: notes, check-in, injuries ---------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_notes(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        """The user's own diary for a period (default last_28d): notes (with tags such as
        sick, travel, alcohol, stress) and morning check-ins (1-5 scores). Use it to explain
        unusual numbers (an HRV dip during a cold) before drawing conclusions."""
        span = window(period, start, end, "last_28d")
        data = {
            "notes": journal.notes(engine, span, tag),
            "checkins": journal.checkins(engine, span),
            "checkin_scales": journal.CHECKIN_FIELDS,
            "suggested_tags": journal.SUGGESTED_TAGS,
        }
        return envelope(data, span=span)

    @tool(WRITE, "diary.write")
    def vibesynergy_add_note(
        text: str,
        date: str | None = None,
        end: str | None = None,
        tags: list[str] | None = None,
        activity_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a note in the user's diary: for one day (date, default today), a span (date
        to end, e.g. a trip or an illness) or one activity (activity_id). Add tags such as
        sick, travel, alcohol, stress, heat, bad_sleep, race. Only save what the user said
        or confirmed; for pain or an injury use vibesynergy_log_injury instead."""
        start_day = _date(date) if date else today()
        end_day = _date(end) if end else None
        if activity_id:
            activity_id = views.resolve_activity(engine, activity_id)
        saved = journal.add_note(
            engine,
            text=text,
            start=start_day,
            end=end_day,
            tags=tags,
            activity_id=activity_id,
            source=note_source(),
        )
        from vibesynergy.query import icu_notes

        # Opt-in and only when intervals.icu keeps wellness comments private.
        icu_notes.schedule(engine, rt.settings.data_dir, {start_day})
        return diary_changed(saved)

    @tool(DESTRUCTIVE, "diary.write")
    def vibesynergy_edit_note(
        id: str,
        text: str | None = None,
        tags: list[str] | None = None,
        date: str | None = None,
        end: str | None = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        """Change or delete (delete=true) one of the user's notes, by its id from
        vibesynergy_notes. Only when the user asks for it."""
        if delete:
            journal.delete_note(engine, id)
            return diary_changed({"deleted": id})
        return diary_changed(
            journal.edit_note(
                engine,
                id,
                text=text,
                tags=tags,
                start=_date(date) if date else None,
                end=_date(end) if end else None,
            )
        )

    @tool(WRITE, "diary.write")
    def vibesynergy_checkin(
        date: str | None = None,
        sleep_quality: int | None = None,
        soreness: int | None = None,
        mood: int | None = None,
        stress: int | None = None,
        motivation: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Save (part of) the user's morning check-in for a day (default today). Scores 1-5:
        sleep_quality, mood and motivation 1 = very poor/low … 5 = very good/high;
        soreness and stress 1 = none … 5 = very high. Fields left out keep their value."""
        when = _date(date) if date else today()
        return diary_changed(
            journal.set_checkin(
                engine,
                when,
                sleep_quality=sleep_quality,
                soreness=soreness,
                mood=mood,
                stress=stress,
                motivation=motivation,
                note=note,
            ),
            span=Window(when, when),
        )

    @tool(READ, "read")
    def vibesynergy_injuries(include_resolved: bool = False, days: int = 42) -> dict[str, Any]:
        """The user's injury log: open complaints (and resolved ones with
        include_resolved) with body part, side, status, the pain log of the last `days`
        days and warning flags from fixed rules (pain above 3/10, worse 3 days running, pain
        that changes movement, a returning complaint, no recent update). Relay 'stop'
        flags plainly. Never diagnose: suggest a physiotherapist or doctor for real pain."""
        data = {
            "injuries": journal.injuries(
                engine, today(), include_resolved=include_resolved, days=days
            ),
            "body_parts": journal.BODY_PARTS,
            "note": journal.NOT_A_DIAGNOSIS,
        }
        warnings = [
            f"{i['title']}: {f['message']}"
            for i in data["injuries"]
            for f in i["flags"]
            if f["level"] in ("stop", "warn")
        ]
        return envelope(data, warnings=warnings)

    @tool(WRITE, "diary.write")
    def vibesynergy_log_injury(
        body_part: str | None = None,
        side: str | None = None,
        injury_id: str | None = None,
        pain: int | None = None,
        timing: str | None = None,
        affects_movement: bool | None = None,
        note: str | None = None,
        kind: str | None = None,
        title: str | None = None,
        status: str | None = None,
        notes: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log pain or an injury the way the user says it ("left calf tight after the run,
        3/10"). Identify it by injury_id, or by body_part + side (left, right, both, none):
        the open injury there is used, or a new one starts. pain 0-10 is that day's worst
        pain; timing: during, after, morning or rest; affects_movement if it changes how
        they move. status: active, improving, resolved. notes: diagnosis, physio advice or
        return-to-run steps (appended). kind: niggle, pain or injury. Ask for side and pain
        when missing. Returns the injury with its warning flags; relay them."""
        out = journal.log_injury(
            engine,
            today=today(),
            injury_id=injury_id,
            body_part=body_part,
            side=side,
            kind=kind,
            title=title,
            when=_date(date) if date else None,
            pain=pain,
            timing=timing,
            affects_movement=affects_movement,
            note_text=note,
            status=status,
            notes_text=notes,
        )
        warnings = [f["message"] for f in out["flags"] if f["level"] in ("stop", "warn")]
        return diary_changed(out, warnings=warnings)
