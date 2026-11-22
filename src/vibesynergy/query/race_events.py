"""Races and events: add, change, delete and undo, for the MCP and the portal (#91).

VibeSynergy is the only source of races and of the other events that matter to the plan
(a group training, a camp, a holiday, a time you are not available). They live in the
``race`` table (``kind`` says which); intervals.icu gets a copy (``race_mirror``) and a
change made there never comes back. Garmin is read only: its events are matched, never
written.

Every write path goes through ``validate`` (one check for the MCP and the portal) and the
dedupe: a race or event that is already in VibeSynergy on that day with that name and sport
is refused, and a Garmin or intervals.icu event with the same date, name and sport becomes a
linked copy instead of a second race (``links``). Every write needs a reason and stores a
before and after snapshot on ``audit_log`` (``ACTOR``), so ``revert`` can undo it; a delete
keeps the row (``deleted_at``). Who may write without the athlete's yes is decided before
this module (``plan_proposals.gate``): the athlete's own request writes at once, the AI
coach's own initiative becomes a proposal.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import Engine, insert, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import races
from vibesynergy.store.schema import audit_log, coach_decision_log, icu_event, planned_item, race

RACE = races.RACE
EVENT_KINDS = ("group_training", "camp", "holiday", "unavailable", "other")
KINDS = (RACE, *EVENT_KINDS)
SPORTS = races.SPORTS
PRIORITIES = races.PRIORITIES
FIELDS = (
    "name",
    "date",
    "end_date",
    "kind",
    "sport",
    "distance_m",
    "priority",
    "start_time",
    "location",
    "url",
    "goal_time",
    "notes",
)
ACTOR = "race"  # audit_log.actor of the revertable snapshots
ICU_CATEGORIES = ("RACE_A", "RACE_B", "RACE_C", "NOTE")  # what an intervals.icu copy can be

# Input limits: field sizes, not coaching values.
_TEXT_MAX = {"name": 200, "location": 200, "url": 500, "notes": 2000}
_MAX_SPAN_DAYS = 366
_MAX_DISTANCE_M = 1_000_000.0
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DURATION = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


class RaceError(ValueError):
    """Nothing was changed. The message says what to fix, for a person or a model; ``code``
    names the problem for the portal (``duplicate``, ``changed_since``), with ``values``."""

    def __init__(self, message: str, code: str | None = None, **values: Any) -> None:
        super().__init__(message)
        self.code = code
        self.values = values


# Validation (one function for every write path) ---------------------------------------


def _text(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RaceError(f"{key} must be text")
    value = value.strip()
    if len(value) > _TEXT_MAX.get(key, 200):
        raise RaceError(f"{key} is longer than {_TEXT_MAX.get(key, 200)} characters")
    return value or None


def _day(value: Any, key: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise RaceError(f"{key}: dates look like 2027-04-11, got {value!r}") from exc


def _distance(values: dict[str, Any]) -> float | None:
    if values.get("distance_km") not in (None, ""):
        raw, factor = values["distance_km"], 1000.0
    else:
        raw, factor = values.get("distance_m"), 1.0
    if raw is None or raw == "":
        return None
    try:
        metres = float(str(raw).replace(",", ".")) * factor
    except ValueError as exc:
        raise RaceError(f"distance must be a number, got {raw!r}") from exc
    if not 0 < metres <= _MAX_DISTANCE_M:
        raise RaceError("distance must be more than 0 and at most 1000 km")
    return round(metres, 1)


def validate(values: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """The checked row for a new race or event (``current`` None) or a change to one.

    ``values`` holds what is set: a missing key (or None) keeps the current value, an empty
    string clears it. ``distance_km`` may stand in for ``distance_m``. Raises ``RaceError``
    with a message that says what to fix."""
    given = {k: v for k, v in values.items() if v is not None}
    merged: dict[str, Any] = {k: (current or {}).get(k) for k in FIELDS}
    for key, value in given.items():
        if key in FIELDS:
            merged[key] = value
    if "distance_km" in given or "distance_m" in given:
        merged["distance_m"] = _distance(given)
    else:
        merged["distance_m"] = _distance({"distance_m": merged.get("distance_m")})
    row: dict[str, Any] = {}
    kind = str(merged.get("kind") or RACE).strip().lower()
    if kind not in KINDS:
        raise RaceError(f"kind must be one of {', '.join(KINDS)}")
    row["kind"] = kind
    name = _text(merged, "name")
    if not name:
        raise RaceError("a name is required")
    row["name"] = name
    day = _day(merged.get("date"), "date")
    if day is None:
        raise RaceError("a date is required")
    row["date"] = day
    end = _day(merged.get("end_date"), "end_date")
    if end is not None:
        if end < day:
            raise RaceError("the end date is before the date")
        if (end - day).days > _MAX_SPAN_DAYS:
            raise RaceError("an event lasts at most a year")
        if end == day:
            end = None
    row["end_date"] = end
    sport = merged.get("sport")
    sport = str(sport).strip().lower() if sport not in (None, "") else None
    if sport is not None and sport not in SPORTS:
        raise RaceError(f"sport must be one of {', '.join(SPORTS)}")
    if kind == RACE and sport is None:
        raise RaceError("a race needs a sport")
    row["sport"] = sport
    row["distance_m"] = merged["distance_m"]
    priority = merged.get("priority")
    priority = str(priority).strip().upper() if priority not in (None, "") else None
    if priority is not None and priority not in PRIORITIES:
        raise RaceError("priority must be A, B or C (or empty)")
    if kind != RACE:
        if priority is not None and given.get("priority") not in (None, ""):
            raise RaceError("only a race has a priority")
        priority = None  # an event that was a race leaves its letter behind
    row["priority"] = priority
    start = _text(merged, "start_time")
    if start:
        m = _TIME.match(start)
        if not m:
            raise RaceError("the start time looks like 09:30 (24-hour clock)")
        start = f"{int(m.group(1)):02d}:{m.group(2)}"
    row["start_time"] = start
    row["location"] = _text(merged, "location")
    url = _text(merged, "url")
    if url and not _URL.match(url):
        raise RaceError("the link must start with https:// or http://")
    row["url"] = url
    goal = _text(merged, "goal_time")
    if goal:
        m = _DURATION.match(goal)
        if not m:
            raise RaceError("the goal time looks like 3:30:00 (hours:minutes:seconds)")
        goal = f"{int(m.group(1))}:{m.group(2)}:{m.group(3)}"
    if goal and kind != RACE:
        raise RaceError("only a race has a goal time")
    row["goal_time"] = goal
    row["notes"] = _text(merged, "notes")
    return row


# Dedupe -------------------------------------------------------------------------------


def _key(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _same_sport(mine: str | None, theirs: str | None) -> bool:
    """A copy's sport is guessed from its name; one it cannot tell ("other") still matches."""
    return mine is None or theirs in (None, "other") or mine == theirs


def _duplicate(conn: Any, row: dict[str, Any], exclude: str | None) -> dict[str, Any] | None:
    """A race or event VibeSynergy already has: same date, name and sport."""
    for other in conn.execute(
        select(race).where(race.c.date == row["date"], race.c.deleted_at.is_(None))
    ).mappings():
        if other["id"] == exclude:
            continue
        if _key(other["name"]) == _key(row["name"]) and (other["sport"] or None) == row["sport"]:
            return dict(other)
    return None


def _twice(same: dict[str, Any], then: str) -> RaceError:
    day = same["date"].isoformat()
    return RaceError(
        f"{same['name']} on {day} is already in VibeSynergy (id {same['id']}): {then}",
        "duplicate",
        name=same["name"],
        date=day,
        id=same["id"],
    )


def _linked(conn: Any, exclude: str | None) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for ident, links in conn.execute(select(race.c.id, race.c.links)):
        if ident == exclude:
            continue
        for target, ref in (links or {}).items():
            if target in ("garmin", "intervals") and ref is not None:
                out.add((target, str(ref)))
    return out


def _own_copy(conn: Any, event_id: str) -> bool:
    from vibesynergy.query.race_mirror import PREFIX

    ext = conn.execute(select(icu_event.c.external_id).where(icu_event.c.id == event_id)).scalar()
    return bool(ext and str(ext).startswith(PREFIX))


def copies(conn: Any, row: dict[str, Any], exclude: str | None = None) -> dict[str, str]:
    """The Garmin and intervals.icu events that are this race or event (same date, name and
    sport) and no other race's copy yet: they become its linked copies, never a second race.
    Garmin is only read."""
    taken = _linked(conn, exclude)
    found: dict[str, str] = {}
    for g in conn.execute(
        select(planned_item.c.source_id, planned_item.c.title, planned_item.c.sport).where(
            planned_item.c.source == "garmin",
            planned_item.c.kind == "event",
            planned_item.c.date == row["date"],
        )
    ):
        if (
            ("garmin", str(g.source_id)) not in taken
            and _key(g.title) == _key(row["name"])
            and _same_sport(row["sport"], races.guess_sport(g.title, g.sport))
        ):
            found["garmin"] = str(g.source_id)
            break
    for e in conn.execute(
        select(icu_event.c.id, icu_event.c.name, icu_event.c.type, icu_event.c.description).where(
            icu_event.c.date == row["date"], icu_event.c.category.in_(ICU_CATEGORIES)
        )
    ):
        if (
            ("intervals", str(e.id)) not in taken
            and _key(e.name) == _key(row["name"])
            and _same_sport(row["sport"], races.guess_sport(e.name, e.type, e.description))
        ):
            found["intervals"] = str(e.id)
            break
    return found


# Reading ------------------------------------------------------------------------------


def public(r: dict[str, Any]) -> dict[str, Any]:
    """One race or event as the MCP and the portal show it."""
    links = r.get("links") or {}
    return {
        "id": r["id"],
        "kind": r.get("kind") or RACE,
        "name": r["name"],
        "date": r["date"].isoformat(),
        "end_date": r["end_date"].isoformat() if r.get("end_date") else None,
        "sport": r.get("sport"),
        "distance_m": r.get("distance_m"),
        "priority": r.get("priority") if (r.get("kind") or RACE) == RACE else None,
        "start_time": r.get("start_time"),
        "location": r.get("location"),
        "url": r.get("url"),
        "goal_time": r.get("goal_time"),
        "notes": r.get("notes"),
        "copies": {k: v for k, v in links.items() if k in ("garmin", "intervals") and v},
        "origin": r.get("origin"),
        "deleted": r.get("deleted_at") is not None,
    }


def get(engine: Engine, ident: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        found = conn.execute(select(race).where(race.c.id == ident)).mappings().first()
    return dict(found) if found else None


def listing(
    engine: Engine,
    *,
    since: date | None = None,
    until: date | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Races and events (not deleted) that touch ``since`` to ``until``, oldest first.
    ``kind`` ``race`` or ``event`` narrows to one of both."""
    with engine.connect() as conn:
        found = [
            dict(r)
            for r in conn.execute(
                select(race).where(race.c.deleted_at.is_(None)).order_by(race.c.date, race.c.id)
            ).mappings()
        ]
    out = []
    for r in found:
        last = r.get("end_date") or r["date"]
        if since is not None and last < since:
            continue
        if until is not None and r["date"] > until:
            continue
        is_race = (r.get("kind") or RACE) == RACE
        if (kind == RACE and not is_race) or (kind == "event" and is_race):
            continue
        out.append(public(r))
    return out


# Writing ------------------------------------------------------------------------------


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise RaceError("a reason is required")
    return text[:2000]


def _snapshot(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if r is None:
        return None
    out: dict[str, Any] = {k: r.get(k) for k in FIELDS}
    out["id"] = r["id"]
    out["links"] = dict(r.get("links") or {})
    for key in ("date", "end_date"):
        value = out[key]
        out[key] = value.isoformat() if isinstance(value, date) else None
    return out


def _from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: snap.get(k) for k in FIELDS}
    for key in ("date", "end_date"):
        value = out[key]
        out[key] = date.fromisoformat(str(value)) if value else None
    out["kind"] = out["kind"] or RACE
    return out


def _record(
    conn: Any,
    *,
    action: str,
    reason: str,
    author: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> int:
    """The revertable snapshot (``audit_log``) and one "Plan change" on the coach feed."""
    ident = (after or before or {})["id"]
    now = utc_now()
    audit_id = conn.execute(
        insert(audit_log)
        .values(
            at=now,
            actor=ACTOR,
            action=f"race.{action}",
            target=f"race:{ident}",
            author=author,
            outcome="ok",
            detail={
                "action": f"race.{action}",
                "race_id": ident,
                "reason": reason,
                "before": before,
                "after": after,
                "revertable": True,
                **(extra or {}),
            },
        )
        .returning(audit_log.c.id)
    ).scalar_one()
    shown = after or before or {}
    noun = "race" if (shown.get("kind") or RACE) == RACE else "event"
    verb = {"add": "add", "update": "change", "delete": "delete", "revert": "change"}[action]
    if action == "revert":
        verb = "add" if before is None else "delete" if after is None else "change"
    conn.execute(
        insert(coach_decision_log).values(
            subject_id=races.SUBJECT,
            at=now,
            kind="note",
            author=author or "user",
            entry_type="plan_change",
            for_date=date.fromisoformat(shown["date"]) if shown.get("date") else None,
            text=reason[:4000],
            meta={
                "action": f"plan.{noun}_{verb}",
                "plan": {
                    "action": f"{noun}_{verb}",
                    "name": shown.get("name"),
                    "date": shown.get("date"),
                    "sport": shown.get("sport"),
                    "kind": shown.get("kind"),
                    "race_id": ident,
                    "audit_id": audit_id,
                },
            },
        )
    )
    return int(audit_id)


def _expect(row: dict[str, Any], expect: str | None) -> None:
    is_race = (row.get("kind") or RACE) == RACE
    if expect == RACE and not is_race:
        raise RaceError("this is an event, not a race: add it with vibesynergy_event_add")
    if expect == "event" and is_race:
        raise RaceError("this is a race: add it with vibesynergy_race_add")


def add(
    engine: Engine,
    values: dict[str, Any],
    *,
    reason: str,
    author: str,
    expect: str | None = None,
    origin: str = "vibesynergy",
) -> dict[str, Any]:
    """Add one race or event. ``expect`` (``race`` or ``event``) refuses the other kind."""
    why = _reason(reason)
    row = validate(values)
    _expect({**row, "id": row["name"]}, expect)
    now = utc_now()
    with engine.begin() as conn:
        same = _duplicate(conn, row, None)
        if same is not None:
            raise _twice(same, "change that one instead")
        ids = {r for (r,) in conn.execute(select(race.c.id))}
        ident = base = races.slug(row["date"], row["name"])
        n = 2
        while ident in ids:
            ident, n = f"{base}-{n}", n + 1
        links = copies(conn, row)
        conn.execute(
            insert(race).values(
                id=ident,
                subject_id=races.SUBJECT,
                links=links,
                origin=origin,
                created_at=now,
                updated_at=now,
                **row,
            )
        )
        stored = dict(conn.execute(select(race).where(race.c.id == ident)).mappings().one())
        audit_id = _record(
            conn, action="add", reason=why, author=author, before=None, after=_snapshot(stored)
        )
    return {"race": public(stored), "audit_id": audit_id, "linked": links}


def _live(conn: Any, ident: str) -> dict[str, Any]:
    found = conn.execute(select(race).where(race.c.id == ident)).mappings().first()
    if found is None or found["deleted_at"] is not None:
        raise RaceError(f"there is no race or event {ident!r}")
    return dict(found)


def change(
    engine: Engine,
    ident: str,
    values: dict[str, Any],
    *,
    reason: str,
    author: str,
    expect: str | None = None,
) -> dict[str, Any]:
    """Change one race or event: what ``values`` sets (None keeps, "" clears)."""
    why = _reason(reason)
    with engine.begin() as conn:
        current = _live(conn, ident)
        _expect(current, expect)
        row = validate(values, current)
        same = _duplicate(conn, row, ident)
        if same is not None:
            raise _twice(same, "change that one instead")
        if all(row[k] == current.get(k) for k in FIELDS):
            return {"race": public(current), "audit_id": None, "changed": False}
        links = dict(current.get("links") or {})
        if row["date"] != current["date"] or _key(row["name"]) != _key(current["name"]):
            # A Garmin event, or an intervals.icu event VibeSynergy did not make, is the copy
            # of the day and name it had. VibeSynergy's own intervals.icu copy follows the
            # change (race_mirror).
            links.pop("garmin", None)
            if links.get("intervals") and not _own_copy(conn, str(links["intervals"])):
                links.pop("intervals")
        for target, ref in copies(conn, row, ident).items():
            links.setdefault(target, ref)
        conn.execute(
            update(race).where(race.c.id == ident).values(links=links, updated_at=utc_now(), **row)
        )
        stored = dict(conn.execute(select(race).where(race.c.id == ident)).mappings().one())
        audit_id = _record(
            conn,
            action="update",
            reason=why,
            author=author,
            before=_snapshot(current),
            after=_snapshot(stored),
        )
    return {"race": public(stored), "audit_id": audit_id, "changed": True}


def delete(
    engine: Engine, ident: str, *, reason: str, author: str, expect: str | None = None
) -> dict[str, Any]:
    """Delete one race or event. The row stays (``deleted_at``), so ``revert`` brings it
    back; the mirror removes its intervals.icu copy. Garmin is never touched."""
    why = _reason(reason)
    with engine.begin() as conn:
        current = _live(conn, ident)
        _expect(current, expect)
        conn.execute(update(race).where(race.c.id == ident).values(deleted_at=utc_now()))
        audit_id = _record(
            conn,
            action="delete",
            reason=why,
            author=author,
            before=_snapshot(current),
            after=None,
        )
    return {"race": {**public(current), "deleted": True}, "audit_id": audit_id}


def revert(engine: Engine, audit_id: int, *, reason: str, author: str) -> dict[str, Any]:
    """Undo one race or event change (its ``audit_id``): an add is deleted, a delete comes
    back, a change gets its earlier values. Refused when the race changed after it (undo
    that later change first) or when it would make a duplicate."""
    why = _reason(reason)
    with engine.begin() as conn:
        found = (
            conn.execute(
                select(audit_log).where(audit_log.c.id == int(audit_id), audit_log.c.actor == ACTOR)
            )
            .mappings()
            .first()
        )
        if found is None:
            raise RaceError(f"there is no race or event change {audit_id}")
        detail = found["detail"] or {}
        if not detail.get("revertable"):
            raise RaceError(f"change {audit_id} cannot be undone")
        before, after = detail.get("before"), detail.get("after")
        ident = (after or before or {}).get("id")
        current = conn.execute(select(race).where(race.c.id == ident)).mappings().first()
        if current is None:
            raise RaceError(f"there is no race or event {ident!r}")
        current = dict(current)
        now_snap = None if current["deleted_at"] is not None else _snapshot(current)
        if _comparable(now_snap) != _comparable(after):
            raise RaceError(
                f"{current['name']} changed after change {audit_id}: undo the later change first",
                "changed_since",
                name=current["name"],
            )
        if before is None:
            conn.execute(update(race).where(race.c.id == ident).values(deleted_at=utc_now()))
        else:
            row = _from_snapshot(before)
            same = _duplicate(conn, row, ident)
            if same is not None:
                raise _twice(same, "undoing would make it twice")
            conn.execute(
                update(race)
                .where(race.c.id == ident)
                .values(deleted_at=None, updated_at=utc_now(), **row)
            )
        stored = dict(conn.execute(select(race).where(race.c.id == ident)).mappings().one())
        new_id = _record(
            conn,
            action="revert",
            reason=why,
            author=author,
            before=now_snap,
            after=None if before is None else _snapshot(stored),
            extra={"reverts": int(audit_id)},
        )
    return {
        "race": public(stored),
        "audit_id": new_id,
        "reverts": int(audit_id),
    }


def _comparable(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """A snapshot without its links: a copy linked later is no change to undo first."""
    if snap is None:
        return None
    return {k: snap.get(k) for k in ("id", *FIELDS)}


def history(engine: Engine, ident: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """The latest race and event changes (newest first), with the audit_id to undo one."""
    query = select(audit_log).where(audit_log.c.actor == ACTOR)
    if ident:
        query = query.where(audit_log.c.target == f"race:{ident}")
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(audit_log.c.id.desc()).limit(limit)).mappings()
        out = []
        for r in rows:
            detail = r["detail"] or {}
            shown = detail.get("after") or detail.get("before") or {}
            out.append(
                {
                    "audit_id": r["id"],
                    "at": r["at"].isoformat(),
                    "action": r["action"],
                    "race_id": detail.get("race_id"),
                    "name": shown.get("name"),
                    "date": shown.get("date"),
                    "author": r["author"],
                    "reason": detail.get("reason"),
                }
            )
    return out


def describe(engine: Engine, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What a proposed race or event change would do, checked without writing, for the
    athlete under Waiting for you (``plan_proposals``)."""
    a = {k: v for k, v in arguments.items() if k != "race_id"}
    if action == "add":
        row = validate(a)
        noun = "race" if row["kind"] == RACE else "event"
        with engine.connect() as conn:
            same = _duplicate(conn, row, None)
        if same is not None:
            raise _twice(same, "change that one instead")
        return _summary(f"{noun}_add", row)
    if action == "revert":
        with engine.connect() as conn:
            found = (
                conn.execute(
                    select(audit_log).where(
                        audit_log.c.id == int(arguments.get("audit_id") or 0),
                        audit_log.c.actor == ACTOR,
                    )
                )
                .mappings()
                .first()
            )
        if found is None:
            raise RaceError(f"there is no race or event change {arguments.get('audit_id')}")
        detail = found["detail"] or {}
        shown = detail.get("before") or detail.get("after") or {}
        noun = "race" if (shown.get("kind") or RACE) == RACE else "event"
        return {**_summary(f"{noun}_revert", _from_snapshot(shown))}
    with engine.connect() as conn:
        current = _live(conn, str(arguments.get("race_id") or ""))
    noun = "race" if (current.get("kind") or RACE) == RACE else "event"
    if action == "delete":
        return _summary(f"{noun}_delete", current)
    row = validate(a, current)
    out = _summary(f"{noun}_change", row)
    if row["date"] != current["date"]:
        out["from_date"] = current["date"].isoformat()
    return out


def _summary(action: str, row: dict[str, Any]) -> dict[str, Any]:
    day = row.get("date")
    return {
        "action": action,
        "name": row.get("name"),
        "date": day.isoformat() if isinstance(day, date) else day,
        "sport": row.get("sport"),
        "kind": row.get("kind"),
        "priority": row.get("priority"),
    }


def upcoming_events(engine: Engine, today: date) -> list[dict[str, Any]]:
    """Events (not races) that are not over yet, for the AI coach's state."""
    return listing(engine, since=today, kind="event")
