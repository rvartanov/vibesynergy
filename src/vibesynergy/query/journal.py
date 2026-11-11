"""Your own diary: notes, the morning check-in and the injury log (phase 6).

Everything here is entered by you, so every write validates its input and raises
``ValueError`` with a message written for the person (or the AI) that entered it. The
warnings on injuries are fixed rules, not a diagnosis: they say when to back off and when to
see someone, never what the problem is.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.core.user_datetime import format_user_date
from vibesynergy.i18n import ENGLISH, Translator
from vibesynergy.store.schema import (
    activity,
    activity_link,
    activity_unified,
    checkin,
    injury,
    injury_log,
    note,
)

log = logging.getLogger(__name__)

SUBJECT = 1

# Suggested tags; others are allowed. Analyses group and exclude by tag (e.g. sick days).
SUGGESTED_TAGS = [
    "sick",
    "injury",
    "pain",  # with the pain fields, the portal also logs it in the injury log
    "travel",
    "alcohol",
    "stress",
    "work",
    "heat",
    "bad_sleep",
    "race",
    "vacation",
    "coach_question",  # a question for the coach: answered in its next run
]
MAX_TEXT = 5000

# Days with these tags are left out of personal baselines (and so of anomaly detection's
# norm) by default: being ill is not the normal to compare against.
BASELINE_EXCLUDE_TAGS = ("sick",)

CHECKIN_FIELDS = {
    "sleep_quality": "1 = very poor … 5 = very good",
    "soreness": "1 = none … 5 = very sore",
    "mood": "1 = very low … 5 = very good",
    "stress": "1 = none … 5 = very high",
    "motivation": "1 = none … 5 = very high",
}

# The body map's regions.
BODY_PARTS = [
    "head",
    "neck",
    "shoulder",
    "upper_back",
    "chest",
    "elbow",
    "wrist",
    "hand",
    "abdomen",
    "lower_back",
    "hip",
    "groin",
    "glute",
    "hamstring",
    "quad",
    "knee",
    "it_band",
    "shin",
    "calf",
    "achilles",
    "ankle",
    "foot",
    "heel",
    "toe",
    "other",
]
SIDES = ["left", "right", "both", "none"]
KINDS = ["niggle", "pain", "injury"]
STATUSES = ["active", "improving", "resolved"]
TIMINGS = ["during", "after", "morning", "rest"]

# Warning rules (see injury_flags).
PAIN_FLAG_ABOVE = 3
WORSENING_DAYS = 3
STALE_DAYS = 7
RECURRENCE_DAYS = 365

NOT_A_DIAGNOSIS = (
    "This is not a diagnosis. Pain that is sharp, keeps getting worse, or changes how you move: "
    "see a physiotherapist or doctor."
)


# Notes ----------------------------------------------------------------------------------


def add_note(
    engine: Engine,
    *,
    text: str,
    start: date,
    end: date | None = None,
    tags: list[str] | None = None,
    activity_id: str | None = None,
    source: str = "cli",
    source_ref: str | None = None,
) -> dict[str, Any]:
    """Add a note, or update it when ``source``/``source_ref`` already exists (imports)."""
    text = _text(text)
    end = end or start
    if end < start:
        raise ValueError("end is before start")
    now = utc_now()
    values = {
        "subject_id": SUBJECT,
        "start_date": start,
        "end_date": end,
        "activity_id": activity_id,
        "text": text,
        "tags": _tags(tags),
        "source": source,
        "source_ref": source_ref,
        "updated_at": now,
    }
    with engine.begin() as conn:
        existing = None
        if source_ref:
            existing = conn.execute(
                select(note.c.id).where(note.c.source == source, note.c.source_ref == source_ref)
            ).scalar()
        if existing:
            conn.execute(update(note).where(note.c.id == existing).values(**values))
            note_id = existing
        else:
            note_id = new_id("note")
            conn.execute(note.insert().values(id=note_id, created_at=now, **values))
    return get_note(engine, note_id)


def edit_note(
    engine: Engine,
    note_id: str,
    *,
    text: str | None = None,
    tags: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    current = get_note(engine, note_id)
    values: dict[str, Any] = {"updated_at": utc_now()}
    if text is not None:
        values["text"] = _text(text)
    if tags is not None:
        values["tags"] = _tags(tags)
    new_start = start or date.fromisoformat(current["start_date"])
    new_end = end or (start if start and not end else date.fromisoformat(current["end_date"]))
    if new_end < new_start:
        raise ValueError("end is before start")
    values["start_date"], values["end_date"] = new_start, new_end
    with engine.begin() as conn:
        conn.execute(update(note).where(note.c.id == note_id).values(**values))
    return get_note(engine, note_id)


def delete_note(engine: Engine, note_id: str) -> None:
    get_note(engine, note_id)
    with engine.begin() as conn:
        conn.execute(delete(note).where(note.c.id == note_id))


def get_note(engine: Engine, note_id: str) -> dict[str, Any]:
    rows = _rows(engine, select(note).where(note.c.id == note_id))
    if not rows:
        raise ValueError(f"no note {note_id!r}")
    return _note_out(rows[0])


def notes(engine: Engine, window: Window, tag: str | None = None) -> list[dict[str, Any]]:
    """Notes that overlap the window (a trip that started earlier counts), oldest first."""
    rows = _rows(
        engine,
        select(note)
        .where(note.c.start_date <= window.end, note.c.end_date >= window.start)
        .order_by(note.c.start_date, note.c.created_at),
    )
    out = [_note_out(r) for r in rows]
    if tag:
        wanted = tag.strip().lower()
        out = [n for n in out if wanted in n["tags"]]
    return _with_activity(engine, out)


def _with_activity(engine: Engine, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each note tied to an activity that workout's date, sport and name: a text
    imported from Strava or Garmin means little in the diary without them. ``activity`` is
    None when the activity is gone (merged away or deleted)."""
    groups = {n["activity_id"] for n in items if n.get("activity_id")}
    if not groups:
        return items
    t = activity_unified
    with engine.connect() as conn:
        found = {
            r.group_id: {
                "id": r.group_id,
                "date": r.local_date.isoformat(),
                "sport": r.sport,
                "name": r.name,
            }
            for r in conn.execute(
                select(t.c.group_id, t.c.local_date, t.c.sport, t.c.name).where(
                    t.c.group_id.in_(groups)
                )
            )
        }
        # A note may carry a source id ("strava:123") instead of the group id.
        missing = [g for g in groups if g not in found and ":" in g]
        aliases: dict[str, str] = {}
        for ident in missing:
            source, _, source_id = ident.partition(":")
            group = conn.execute(
                select(activity_link.c.group_id).where(
                    activity_link.c.source == source, activity_link.c.source_id == source_id
                )
            ).scalar()
            if group:
                aliases[ident] = group
        if aliases:
            for r in conn.execute(
                select(t.c.group_id, t.c.local_date, t.c.sport, t.c.name).where(
                    t.c.group_id.in_(set(aliases.values()))
                )
            ):
                entry = {
                    "id": r.group_id,
                    "date": r.local_date.isoformat(),
                    "sport": r.sport,
                    "name": r.name,
                }
                for ident, group in aliases.items():
                    if group == r.group_id:
                        found[ident] = entry
    for n in items:
        if n.get("activity_id"):
            n["activity"] = found.get(n["activity_id"])
    return items


def notes_for_activity(engine: Engine, activity_id: str) -> list[dict[str, Any]]:
    """Notes tied to one activity (its group id), oldest first: what you wrote about it, plus
    the personal part of a Strava description, so the workout shows the same diary entry the
    Diary page links back to it."""
    rows = _rows(
        engine,
        select(note)
        .where(note.c.activity_id == activity_id)
        .order_by(note.c.start_date, note.c.created_at),
    )
    return [_note_out(r) for r in rows]


def tagged_days(engine: Engine, window: Window, tags: list[str]) -> set[date]:
    """Every date in the window covered by a note with one of these tags (e.g. sick days,
    to leave out of baselines or to explain an anomaly)."""
    wanted = {t.lower() for t in tags}
    days: set[date] = set()
    for n in notes(engine, window):
        if wanted & set(n["tags"]):
            d = max(date.fromisoformat(n["start_date"]), window.start)
            last = min(date.fromisoformat(n["end_date"]), window.end)
            while d <= last:
                days.add(d)
                d += timedelta(days=1)
    return days


# Check-in -------------------------------------------------------------------------------


def set_checkin(engine: Engine, when: date, **scores: Any) -> dict[str, Any]:
    """Set (part of) a day's check-in; fields left out keep their value.

    A blank note does not create a row. On a day that already has a check-in, a blank
    note clears the stored note.
    """
    text = scores.pop("note", None)
    unknown = set(scores) - set(CHECKIN_FIELDS)
    if unknown:
        raise ValueError(f"unknown check-in fields: {sorted(unknown)}")
    values = {k: v for k, v in scores.items() if v is not None}
    for k, v in values.items():
        if not isinstance(v, int) or not 1 <= v <= 5:
            raise ValueError(f"{k} must be a whole number from 1 to 5 ({CHECKIN_FIELDS[k]})")
    with engine.begin() as conn:
        existing = conn.execute(
            select(checkin.c.date).where(checkin.c.subject_id == SUBJECT, checkin.c.date == when)
        ).first()
        if text is not None:
            if text.strip():
                values["note"] = _text(text)
            elif existing is not None:
                values["note"] = None
        if not values:
            raise ValueError(f"give at least one of {', '.join(CHECKIN_FIELDS)} or a note")
        values["updated_at"] = utc_now()
        conn.execute(
            insert(checkin)
            .values(subject_id=SUBJECT, date=when, **values)
            .on_conflict_do_update(index_elements=["subject_id", "date"], set_=values)
        )
    saved = checkins(engine, Window(when, when))[0]
    try:
        from vibesynergy.query.icu_wellness import schedule_checkin_wellness

        schedule_checkin_wellness(engine, when)
    except Exception:
        log.warning("check-in was saved; intervals.icu wellness was not scheduled")
    return saved


def checkins(engine: Engine, window: Window) -> list[dict[str, Any]]:
    rows = _rows(
        engine,
        select(checkin)
        .where(checkin.c.date.between(window.start, window.end))
        .order_by(checkin.c.date),
    )
    return [{k: v for k, v in r.items() if k not in ("subject_id",)} for r in rows]


# Injuries -------------------------------------------------------------------------------


def log_injury(
    engine: Engine,
    *,
    today: date,
    injury_id: str | None = None,
    body_part: str | None = None,
    side: str | None = None,
    kind: str | None = None,
    title: str | None = None,
    when: date | None = None,
    pain: int | None = None,
    timing: str | None = None,
    affects_movement: bool | None = None,
    note_text: str | None = None,
    status: str | None = None,
    notes_text: str | None = None,
) -> dict[str, Any]:
    """One call for everything about an injury, the way you would say it:

    - identify it by ``injury_id``, or by ``body_part`` + ``side``: the open (active or
      improving) injury there is used, otherwise a new one is started;
    - ``pain`` (0-10) with ``timing`` logs that day's worst pain (``when``, default today);
    - ``status`` moves it along (``resolved`` sets the resolved date);
    - ``notes_text`` is added to its notes (diagnosis, physio advice, return-to-run steps).
    """
    when = when or today
    if when > today:
        raise ValueError("date is in the future")
    now = utc_now()
    with engine.begin() as conn:
        if injury_id:
            row = conn.execute(select(injury).where(injury.c.id == injury_id)).mappings().first()
            if not row:
                raise ValueError(f"no injury {injury_id!r}; see vibesynergy_injuries")
            row = dict(row)
        else:
            if not body_part:
                raise ValueError("give injury_id, or body_part (and side) to find or start one")
            body_part = _choice(body_part, BODY_PARTS, "body_part")
            side = _choice(side or "none", SIDES, "side")
            row = (
                conn.execute(
                    select(injury)
                    .where(
                        injury.c.body_part == body_part,
                        injury.c.side == side,
                        injury.c.status != "resolved",
                    )
                    .order_by(injury.c.onset.desc())
                )
                .mappings()
                .first()
            )
            row = dict(row) if row else None
            if row is None:
                if status == "resolved":
                    raise ValueError(f"no open injury at {side} {body_part} to resolve")
                row = {
                    "id": new_id("inj"),
                    "subject_id": SUBJECT,
                    "body_part": body_part,
                    "side": side,
                    "kind": _choice(kind or "niggle", KINDS, "kind"),
                    "title": (title or _default_title(body_part, side)).strip()[:200],
                    "onset": when,
                    "status": "active",
                    "resolved_on": None,
                    "notes": "",
                    "created_at": now,
                    "updated_at": now,
                }
                conn.execute(injury.insert().values(**row))

        changes: dict[str, Any] = {}
        if kind and row["kind"] != kind:
            changes["kind"] = _choice(kind, KINDS, "kind")
        if title and injury_id:
            changes["title"] = title.strip()[:200]
        if status:
            status = _choice(status, STATUSES, "status")
            changes["status"] = status
            changes["resolved_on"] = when if status == "resolved" else None
        if notes_text and notes_text.strip():
            entry = f"{when}: {_text(notes_text)}"
            changes["notes"] = f"{row['notes']}\n{entry}".strip()
        if changes:
            changes["updated_at"] = now
            conn.execute(update(injury).where(injury.c.id == row["id"]).values(**changes))

        if pain is not None:
            if not isinstance(pain, int) or not 0 <= pain <= 10:
                raise ValueError("pain must be a whole number from 0 (none) to 10 (worst)")
            values = {
                "pain": pain,
                "timing": _choice(timing or "rest", TIMINGS, "timing"),
                "affects_movement": bool(affects_movement),
                "note": _text(note_text) if note_text and note_text.strip() else None,
                "updated_at": now,
            }
            conn.execute(
                insert(injury_log)
                .values(injury_id=row["id"], date=when, **values)
                .on_conflict_do_update(index_elements=["injury_id", "date"], set_=values)
            )
        elif not changes and injury_id:
            raise ValueError("nothing to change: give pain, status or notes_text")
    return _injury_out(engine, row["id"], today)


def injuries(
    engine: Engine,
    today: date,
    *,
    include_resolved: bool = False,
    days: int = 42,
    tr: Translator = ENGLISH,
) -> list[dict[str, Any]]:
    """Injuries (open ones first, then recently resolved), each with its recent pain log and
    warnings. ``days`` is how much of the pain log to return."""
    query = select(injury.c.id).order_by(injury.c.onset.desc())
    if not include_resolved:
        query = query.where(injury.c.status != "resolved")
    with engine.connect() as conn:
        ids = list(conn.execute(query).scalars())
    out = [_injury_out(engine, i, today, days=days, tr=tr) for i in ids]
    out.sort(key=lambda i: i["onset"], reverse=True)  # newest first,
    out.sort(key=lambda i: i["status"] == "resolved")  # open ones before resolved ones
    return out


def injury_flags(
    item: dict[str, Any],
    log: list[dict[str, Any]],
    earlier: list[dict[str, Any]],
    today: date,
    tr: Translator = ENGLISH,
) -> list[dict[str, str]]:
    """The fixed warning rules. ``log`` is the injury's pain log (oldest first); ``earlier``
    are other injuries at the same place."""
    flags: list[dict[str, str]] = []
    if item["status"] == "resolved":
        return flags
    latest = log[-1] if log else None
    if latest and latest["affects_movement"]:
        flags.append(
            {
                "level": "stop",
                "rule": "affects_movement",
                "message": tr(
                    "Pain changes how you move: stop training that hurts it and see a "
                    "physiotherapist or doctor."
                ),
            }
        )
    if latest and latest["pain"] > PAIN_FLAG_ABOVE:
        flags.append(
            {
                "level": "warn",
                "rule": "pain_above_3",
                "message": tr(
                    "Pain {n}/10 on {date} (above {limit}): avoid what provokes it until it "
                    "settles.",
                    n=latest["pain"],
                    date=format_user_date(date.fromisoformat(latest["date"])),
                    limit=PAIN_FLAG_ABOVE,
                ),
            }
        )
    if len(log) >= WORSENING_DAYS:
        last = log[-WORSENING_DAYS:]
        consecutive = all(
            date.fromisoformat(b["date"]) - date.fromisoformat(a["date"]) == timedelta(days=1)
            for a, b in pairwise(last)
        )
        rising = all(b["pain"] > a["pain"] for a, b in pairwise(last))
        if consecutive and rising:
            flags.append(
                {
                    "level": "warn",
                    "rule": "worsening",
                    "message": tr(
                        "Pain rose {n} days in a row ({values}): back off and consider getting "
                        "it checked.",
                        n=WORSENING_DAYS,
                        values=", ".join(str(e["pain"]) for e in last),
                    ),
                }
            )
    onset = date.fromisoformat(item["onset"])
    recent = [
        e
        for e in earlier
        if e["resolved_on"]
        and timedelta(0)
        <= onset - date.fromisoformat(e["resolved_on"])
        <= timedelta(days=RECURRENCE_DAYS)
    ]
    if recent:
        flags.append(
            {
                "level": "info",
                "rule": "recurring",
                "message": tr(
                    "Same place as {title} (resolved {date}): a returning complaint, worth "
                    "mentioning to a physiotherapist.",
                    title=repr(recent[0]["title"]),
                    date=format_user_date(date.fromisoformat(recent[0]["resolved_on"])),
                ),
            }
        )
    last_entry = date.fromisoformat(latest["date"]) if latest else onset
    if (today - last_entry).days > STALE_DAYS:
        flags.append(
            {
                "level": "info",
                "rule": "no_update",
                "message": tr(
                    "No pain logged for {n} days: log today's pain, or mark it improving or "
                    "resolved.",
                    n=(today - last_entry).days,
                ),
            }
        )
    return flags


# Notes written by coaches and agents (the old external coach, MCP clients) are working logs,
# not the athlete's diary. They stay stored; the diary leaves them out.
_AGENT_SOURCES = ("claude",)


def own_words(item: dict[str, Any]) -> bool:
    source = str(item.get("source") or "")
    return not (source in _AGENT_SOURCES or source.startswith("client:"))


def day_journal(engine: Engine, when: date) -> dict[str, Any]:
    """The diary part of one day: notes, the check-in, and injuries open that day."""
    span = Window(when, when)
    open_that_day = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(injury).where(
                injury.c.onset <= when,
                or_(injury.c.resolved_on.is_(None), injury.c.resolved_on >= when),
            )
        ).mappings()
        for r in rows:
            entry = (
                conn.execute(
                    select(
                        injury_log.c.pain, injury_log.c.timing, injury_log.c.affects_movement
                    ).where(and_(injury_log.c.injury_id == r["id"], injury_log.c.date == when))
                )
                .mappings()
                .first()
            )
            open_that_day.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "body_part": r["body_part"],
                    "side": r["side"],
                    "pain": dict(entry) if entry else None,
                }
            )
    found = checkins(engine, span)
    return {
        "notes": [n for n in notes(engine, span) if own_words(n)],
        "checkin": found[0] if found else None,
        "injuries": open_that_day,
    }


def context(engine: Engine, when: date) -> dict[str, Any] | None:
    """What the diary says about a day, compactly (for explaining an unusual value): notes,
    the check-in and pain logged. None when the diary has nothing for that day."""
    day = day_journal(engine, when)
    out: dict[str, Any] = {}
    if day["notes"]:
        out["notes"] = [{"text": n["text"], "tags": n["tags"]} for n in day["notes"]]
    if day["checkin"]:
        out["checkin"] = {
            k: v for k, v in day["checkin"].items() if k in CHECKIN_FIELDS or k == "note"
        }
    pains = [
        {"injury": i["title"], "pain": i["pain"]["pain"]} for i in day["injuries"] if i["pain"]
    ]
    if pains:
        out["pain"] = pains
    return out or None


# Imported from activities ------------------------------------------------------------------

# Lines that start the automatic parts some tools append to Strava descriptions; the note
# keeps only what was written before them.
GENERATED_MARKERS = re.compile(
    r"^\s*-\s*(persoonlijke ai inzicht|ai insight|garmin stats|quote)\s*:"
    # VibeSynergy's own published block ("[/*]" … "[Generated with VibeSynergy */]").
    r"|^\s*\[/\*.*\]\s*$|^\s*\[VibeSynergy\]\s*$",
    re.I | re.M,
)


# Descriptions written by apps rather than the user (music players and the like).
APP_TEXT = re.compile(
    r"perform\.fm|pr\.fm|recorded on perform|playlist synced|music powered by"
    # Zwift's own route and video texts, e.g. "🗺️ Col du Galibier, Lautaret in Video".
    r"|^\W*🗺|zwift video|\bin video$",
    re.I | re.M,
)


def personal_text(description: str | None) -> str | None:
    """The part of an activity description the user wrote (before generated blocks), or
    None when nothing personal is left."""
    if not description:
        return None
    match = GENERATED_MARKERS.search(description)
    text = (description[: match.start()] if match else description).strip()
    return text if text and not APP_TEXT.search(text) else None


def import_activity_notes(engine: Engine) -> int:
    """Notes from what was already entered with activities: the personal part of Strava
    descriptions (from the API or the account archive, which also has private notes). One
    note per source activity, updated in place on later imports; runs after every sync.

    Garmin's feel and effort (RPE) are no longer copied into the diary: the activity
    and the start page show them as RPE and Feel in the UI language, and MCP readers get
    them in ``effort`` (``vibesynergy_activity``, ``vibesynergy_activities``). Notes this import
    wrote from them earlier (source ``garmin``) are removed here, on the next run.
    """
    description = func.json_extract(activity.c.extra, "$.description")
    private = func.json_extract(activity.c.extra, "$.private_note")
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity.c.source,
                activity.c.source_id,
                activity.c.local_date,
                description,
                private,
                activity_link.c.group_id,
            )
            .select_from(
                activity.outerjoin(
                    activity_link,
                    (activity_link.c.source == activity.c.source)
                    & (activity_link.c.source_id == activity.c.source_id),
                )
            )
            .where(
                ((activity.c.source == "strava") & (description.is_not(None)))
                | (
                    (activity.c.source == "strava_export")
                    & ((description.is_not(None)) | (private.is_not(None)))
                )
            )
        ).all()
        existing = {
            (s, r): (i, t)
            for i, s, r, t in conn.execute(
                select(note.c.id, note.c.source, note.c.source_ref, note.c.text).where(
                    note.c.source.in_(["garmin", "strava"])
                )
            )
        }
    notes: dict[tuple[str, str], tuple[str | None, Any, str | None]] = {}  # key: text, day, group
    strava: dict[str, dict[str, Any]] = {}
    for source, source_id, day, desc, secret, group in rows:
        # One note per Strava activity, from the API's copy and the archive's: the API's
        # description when there is one, and the private note only the archive has.
        entry = strava.setdefault(source_id, {"api": False, "desc": None, "secret": None})
        if source == "strava":
            entry.update(api=True, desc=desc, day=day, group=group)
        else:
            if not entry["api"]:
                entry.update(desc=desc, day=day, group=group)
            entry["secret"] = secret
    for source_id, e in strava.items():
        private = f"Private note: {e['secret']}" if e["secret"] else None
        text = "\n\n".join(t for t in (personal_text(e["desc"]), private) if t)
        notes[("strava", source_id)] = (text or None, e["day"], e["group"])

    written = 0
    for (source, _ref), (note_id, _text_now) in existing.items():
        if source == "garmin":  # feel/effort notes from before
            delete_note(engine, note_id)
            written += 1
    for (source, source_id), (text, day, group) in notes.items():
        if not text:
            if (source, source_id) in existing:  # imported before the rules got stricter
                delete_note(engine, existing[(source, source_id)][0])
                written += 1
            continue
        if (source, source_id) in existing and existing[(source, source_id)][1] == _text(text):
            continue
        add_note(
            engine,
            text=text,
            start=day,
            activity_id=group,
            source=source,
            source_ref=source_id,
        )
        written += 1
    return written


# Helpers --------------------------------------------------------------------------------


def _injury_out(
    engine: Engine, injury_id: str, today: date, days: int = 42, tr: Translator = ENGLISH
) -> dict[str, Any]:
    item = _rows(engine, select(injury).where(injury.c.id == injury_id))[0]
    item.pop("subject_id", None)
    log = _rows(
        engine,
        select(injury_log).where(injury_log.c.injury_id == injury_id).order_by(injury_log.c.date),
    )
    for e in log:
        e.pop("injury_id", None)
        e.pop("updated_at", None)
    earlier = _rows(
        engine,
        select(injury.c.title, injury.c.resolved_on).where(
            injury.c.body_part == item["body_part"],
            injury.c.side == item["side"],
            injury.c.id != injury_id,
        ),
    )
    item["flags"] = injury_flags(item, log, earlier, today, tr)
    cutoff = today - timedelta(days=days)
    item["pain_log"] = [e for e in log if date.fromisoformat(e["date"]) >= cutoff]
    item["worst_pain"] = max((e["pain"] for e in log), default=None)
    item["latest_pain"] = log[-1]["pain"] if log else None
    return item


def _note_out(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("subject_id",)}


def _default_title(body_part: str, side: str) -> str:
    part = body_part.replace("_", " ")
    return part.capitalize() if side == "none" else f"{side.capitalize()} {part}"


def _text(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    return text


def _tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = t.strip().lower().replace(" ", "_")
        if t and t not in out:
            out.append(t[:40])
    return out[:20]


def _choice(value: str, allowed: list[str], name: str) -> str:
    v = value.strip().lower().replace(" ", "_").replace("-", "_")
    if v not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    return v


def _rows(engine: Engine, query) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r._mapping.items()}
            for r in conn.execute(query)
        ]
