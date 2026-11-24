"""The athlete's own words of the last days, per day (#170).

What the athlete wrote himself, for the AI coach's moving context: per day the check-in
note, the complaints logged that day (with their note), the diary notes that are not about
a workout, and per workout its name, public and private text (the newest version,
``workout_texts.texts``) and the linked notes that are not already in those texts.

Notes an AI wrote (``journal.own_words``) are not the athlete's words and are left out. The
private text and the notes are the athlete's alone: they may go to the AI coach as context,
never into anything published.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.store.schema import activity_unified, checkin, injury, injury_log

log = logging.getLogger(__name__)


def _days(start: date, end: date) -> list[date]:
    """``end`` back to ``start``, newest first."""
    return [end - timedelta(days=i) for i in range((end - start).days + 1)]


def _workouts(engine: Engine, start: date, end: date) -> list[dict[str, Any]]:
    t = activity_unified
    with engine.connect() as conn:
        rows = conn.execute(
            select(t.c.group_id, t.c.local_date, t.c.sport, t.c.start_utc)
            .where(t.c.local_date.between(start, end))
            .order_by(t.c.start_utc)
        ).all()
    return [{"id": r.group_id, "date": r.local_date, "sport": r.sport} for r in rows]


def _complaints(engine: Engine, start: date, end: date) -> dict[date, list[dict[str, Any]]]:
    out: dict[date, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                injury_log.c.date,
                injury.c.title,
                injury_log.c.pain,
                injury_log.c.timing,
                injury_log.c.note,
            )
            .join(injury, injury.c.id == injury_log.c.injury_id)
            .where(injury_log.c.date.between(start, end))
            .order_by(injury_log.c.date, injury.c.title)
        ).all()
    for r in rows:
        row: dict[str, Any] = {"title": r.title, "pain": r.pain, "timing": r.timing}
        if (r.note or "").strip():
            row["note"] = r.note.strip()
        out.setdefault(r.date, []).append(row)
    return out


def _checkin_notes(engine: Engine, start: date, end: date) -> dict[date, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(checkin.c.date, checkin.c.note).where(checkin.c.date.between(start, end))
        ).all()
    return {r.date: r.note.strip() for r in rows if (r.note or "").strip()}


def _texts(engine: Engine, group: str, private: bool) -> dict[str, str]:
    from vibesynergy.query import workout_texts

    try:
        found = workout_texts.texts(engine, group)
    except (LookupError, ValueError) as exc:  # a workout merged away meanwhile
        log.warning("own words: texts of %s failed: %s", group, exc)
        return {}
    fields = workout_texts.FIELDS if private else ("name", "public")
    return {f: found[f]["text"] for f in fields if (found[f]["text"] or "").strip()}


def gather(engine: Engine, start: date, end: date, *, private: bool = True) -> list[dict[str, Any]]:
    """One entry per day from ``end`` back to ``start`` (newest first), also a day without
    words (just its ``date``).

    ``private=False`` (the insight, which writes a public line): only the workouts' names
    and public texts; no check-in notes, complaints, diary notes, private texts or workout
    notes."""
    from vibesynergy.query import journal, workout_texts

    window = Window(start, end)
    days: dict[date, dict[str, Any]] = {d: {"date": d.isoformat()} for d in _days(start, end)}
    workouts = _workouts(engine, start, end)
    by_group: dict[str, dict[str, Any]] = {}
    for w in workouts:
        texts = _texts(engine, w["id"], private)
        row: dict[str, Any] = {"id": w["id"], "sport": w["sport"]}
        if texts.get("name"):
            row["name"] = texts["name"]
        # The same paragraph once per workout: a Strava description the diary imported as
        # a note, or a note already taken into the private text (workout_texts.unseen).
        seen: set[str] = set()
        for field in ("public", "private"):
            new = workout_texts.unseen(texts.get(field), seen)
            if new:
                row[field] = new
        row["_seen"] = seen
        by_group[w["id"]] = row
        days[w["date"]].setdefault("workouts", []).append(row)
    if private:
        for d, note in _checkin_notes(engine, start, end).items():
            days[d]["checkin_note"] = note
        for d, rows in _complaints(engine, start, end).items():
            days[d]["complaints"] = rows
        for n in journal.notes(engine, window):
            if not journal.own_words(n):
                continue
            group = (n.get("activity") or {}).get("id")
            target = by_group.get(group) if group else None
            if target is not None:
                new = workout_texts.unseen(n["text"], target["_seen"])
                if new:
                    target.setdefault("notes", []).append(new)
                continue
            # A note over several days (a trip) is given once, on its first day here.
            first = max(date.fromisoformat(n["start_date"]), start)
            row = {"text": n["text"].strip()}
            if n.get("tags"):
                row["tags"] = n["tags"]
            days[first].setdefault("notes", []).append(row)
    for row in by_group.values():
        row.pop("_seen", None)
    return list(days.values())
