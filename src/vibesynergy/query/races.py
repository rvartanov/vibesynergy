"""Races VibeSynergy owns (#91, #111): the only source of races for the advice, the reports,
the dashboard and the AI coach.

A Garmin event or an intervals.icu ``RACE_A/B/C`` event is not a race to VibeSynergy. Those
calendars only carry copies (``links``). Migration 0053 moved the races they held into the
``race`` table once; after that a change made there changes nothing here. ``adopt`` repeats
that copy on request (``vibesynergy plan import-races``): it only adds races that are not
in VibeSynergy yet and never changes or removes one.

The priority is VibeSynergy's own A/B/C letter. The primary race is not stored: it is the
next A race (#120), so it moves on by itself after that race.

The rows have the shape of a ``planned_item`` event (``source`` ``vibesynergy``,
``source_id`` the race id), so the calendar and the race phase read them as before.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import icu_event, planned_item, race, race_priority

SOURCE = "vibesynergy"
SUBJECT = 1
RACE = "race"  # ``race.kind`` of a race; the other kinds are events (``race_events``)
PRIORITIES = frozenset({"A", "B", "C"})
SPORTS = ("run", "ride", "swim", "triathlon", "duathlon", "trail", "other")
ICU_PRIORITY = {"RACE_A": "A", "RACE_B": "B", "RACE_C": "C"}

_SPORT_WORDS = (
    ("triathlon", re.compile(r"triat|ironman", re.IGNORECASE)),
    ("duathlon", re.compile(r"duat", re.IGNORECASE)),
    ("trail", re.compile(r"trail|ultra", re.IGNORECASE)),
    ("swim", re.compile(r"zwem|swim", re.IGNORECASE)),
    ("ride", re.compile(r"fiets|ride|cycl|wielren|gravel|mtb", re.IGNORECASE)),
    ("run", re.compile(r"marathon|\bloop\b|\brun\b|\bkm\b|\d+k\b", re.IGNORECASE)),
)
_SWIM = re.compile(r"zwem|swim")
_BIKE = re.compile(r"fiets|bike|ride|cycl")
_THREE = re.compile(r"\blop|\bloop|\brun")
_ICU_SPORT = {"run": "run", "ride": "ride", "virtualride": "ride", "swim": "swim"}


def guess_sport(title: str | None, icu_type: str | None = None, text: str | None = None) -> str:
    """The race's sport from its name (and the intervals.icu type or text)."""
    lowered = f"{title or ''} {text or ''}".casefold()
    if _THREE.search(lowered) and _SWIM.search(lowered) and _BIKE.search(lowered):
        return "triathlon"  # swim, bike and run in the text: "3,8 km zwemmen / 180 km ..."
    for sport, pattern in _SPORT_WORDS:
        if pattern.search(title or ""):
            return sport
    return _ICU_SPORT.get((icu_type or "").casefold(), "other")


def slug(day: date, name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "race").casefold())
    return f"{day.isoformat()}-{'-'.join(words)[:48].strip('-') or 'race'}"


def _key(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _distance_m(extra: dict[str, Any]) -> float | None:
    target = extra.get("completionTarget") or {}
    if isinstance(target, dict) and target.get("unitType") == "distance":
        value = target.get("value")
        if isinstance(value, (int, float)) and value > 0:
            unit = str(target.get("unit") or "kilometer").casefold()
            return round(float(value) * (1 if unit.startswith("meter") else 1000), 1)
    value = extra.get("distance")
    if isinstance(value, (int, float)) and value >= 100:
        return float(value)
    return None


def _row(r: dict[str, Any], primary_id: str | None) -> dict[str, Any]:
    links = r.get("links") or {}
    extra: dict[str, Any] = {
        "ownership": "vibesynergy_plan",  # a race is part of the plan
        "location": r.get("location"),
        "start_time": r.get("start_time"),
        "url": r.get("url"),
        "goal_time": r.get("goal_time"),
        "notes": r.get("notes"),
        "links": links,
    }
    if r.get("distance_m"):
        extra["distance"] = float(r["distance_m"])
        extra["completionTarget"] = {
            "unit": "kilometer",
            "unitType": "distance",
            "value": round(float(r["distance_m"]) / 1000, 2),
        }
    if links.get("intervals"):
        # The id of the race's copy in intervals.icu, shown as a link; never read back.
        extra["icu_id"] = links["intervals"]
    return {
        "source": SOURCE,
        "source_id": r["id"],
        "race_id": r["id"],
        "date": r["date"],
        "kind": "event",
        "title": r["name"],
        "sport": r.get("sport"),
        "plan_id": None,
        "is_race": True,
        "primary": r["id"] == primary_id,
        "priority": r.get("priority") if r.get("priority") in PRIORITIES else None,
        "extra": extra,
    }


def primary_id(rows: list[dict[str, Any]], today: date) -> str | None:
    """The primary race: the next A race on or after ``today`` (#120)."""
    ahead = sorted(
        (r for r in rows if r.get("priority") == "A" and r["date"] >= today),
        key=lambda r: r["date"],
    )
    return ahead[0]["id"] if ahead else None


def rows(
    engine: Engine, today: date, since: date | None = None, until: date | None = None
) -> list[dict[str, Any]]:
    """The races between ``since`` and ``until`` (both optional), oldest first."""
    with engine.connect() as conn:
        found = [
            dict(r)
            for r in conn.execute(
                select(race)
                .where(race.c.deleted_at.is_(None), race.c.kind == RACE)
                .order_by(race.c.date, race.c.id)
            ).mappings()
        ]
    lead = primary_id(found, today)
    return [
        _row(r, lead)
        for r in found
        if (since is None or r["date"] >= since) and (until is None or r["date"] <= until)
    ]


def event_rows(
    engine: Engine, since: date | None = None, until: date | None = None
) -> list[dict[str, Any]]:
    """VibeSynergy's other events (#91: a group training, a camp, a holiday, a time you are
    not available) that touch ``since`` to ``until``, as calendar rows (``is_race`` false).
    A multi-day event carries ``extra.end`` (the day after its last day) and ``all_day``."""
    with engine.connect() as conn:
        found = [
            dict(r)
            for r in conn.execute(
                select(race)
                .where(race.c.deleted_at.is_(None), race.c.kind != RACE)
                .order_by(race.c.date, race.c.id)
            ).mappings()
        ]
    out = []
    for r in found:
        last = r.get("end_date") or r["date"]
        if (since is not None and last < since) or (until is not None and r["date"] > until):
            continue
        row = _row(r, None)
        row["is_race"] = False
        row["priority"] = None
        row["event_kind"] = r["kind"]
        row["extra"] = {
            **row["extra"],
            "ownership": "vibesynergy_plan",
            "event_kind": r["kind"],
            "all_day": True,
            "end": (last + timedelta(days=1)).isoformat(),
        }
        out.append(row)
    return out


def resolve(engine: Engine, source: str, source_id: str) -> str | None:
    """The race id for a calendar row: a race's own id, or a Garmin or intervals.icu copy."""
    with engine.connect() as conn:
        found = [dict(r) for r in conn.execute(select(race).where(race.c.kind == RACE)).mappings()]
    if source == SOURCE:
        return next((r["id"] for r in found if r["id"] == source_id), None)
    target = {"garmin": "garmin", "intervals_icu": "intervals", "intervals": "intervals"}.get(
        source
    )
    if target is None:
        return None
    return next(
        (r["id"] for r in found if str((r["links"] or {}).get(target) or "") == str(source_id)),
        None,
    )


def set_priority(engine: Engine, race_id: str, priority: str | None) -> dict[str, Any]:
    if priority is not None and priority not in PRIORITIES:
        raise ValueError("priority must be A, B, C or null")
    with engine.begin() as conn:
        done = conn.execute(
            update(race)
            .where(race.c.id == race_id, race.c.deleted_at.is_(None), race.c.kind == RACE)
            .values(priority=priority, updated_at=utc_now())
        ).rowcount
    if not done:
        raise LookupError("race not found")
    return {"source": SOURCE, "source_id": race_id, "priority": priority}


# Copy from the calendars ---------------------------------------------------------------


def candidates(engine: Engine) -> list[dict[str, Any]]:
    """The races Garmin events and intervals.icu RACE_A/B/C events hold, one per race.

    A Garmin race and an intervals.icu race on the same day are one race: the Garmin name,
    date, place and distance; the priority chosen for the Garmin event, else the
    intervals.icu letter, else A for Garmin's primary race.
    """
    with engine.connect() as conn:
        garmin = [
            dict(r)
            for r in conn.execute(
                select(planned_item)
                .where(
                    planned_item.c.source == "garmin",
                    planned_item.c.kind == "event",
                    (planned_item.c.is_race) | (planned_item.c.primary),
                )
                .order_by(planned_item.c.date)
            ).mappings()
        ]
        icu = [
            dict(r)
            for r in conn.execute(
                select(icu_event)
                .where(icu_event.c.category.in_(tuple(ICU_PRIORITY)))
                .order_by(icu_event.c.date)
            ).mappings()
        ]
        saved = {
            (r.source, r.source_id): r.priority
            for r in conn.execute(
                select(race_priority.c.source, race_priority.c.source_id, race_priority.c.priority)
            )
        }
    out: list[dict[str, Any]] = []
    for g in garmin:
        extra = g.get("extra") or {}
        letter = saved.get(("garmin", g["source_id"]))
        when = (extra.get("eventTimeLocal") or {}).get("startTimeHhMm")
        out.append(
            {
                "name": g.get("title") or "Race",
                "date": g["date"],
                "sport": guess_sport(g.get("title"), g.get("sport")),
                "distance_m": _distance_m(extra),
                "priority": letter if letter in PRIORITIES else None,
                "start_time": when if isinstance(when, str) else None,
                "location": extra.get("location"),
                "links": {"garmin": g["source_id"]},
                "_primary": bool(g.get("primary")),
            }
        )
    for e in icu:
        letter = ICU_PRIORITY[e["category"]]
        same = next(
            (c for c in out if c["date"] == e["date"] and "intervals" not in c["links"]), None
        )
        if same is not None:
            same["links"]["intervals"] = str(e["id"])
            if same["priority"] is None:
                same["priority"] = letter
            if same["sport"] == "other":
                same["sport"] = guess_sport(e.get("name"), e.get("type"), e.get("description"))
            if not same["distance_m"]:
                same["distance_m"] = _distance_m(e.get("raw") or {})
            continue
        out.append(
            {
                "name": e.get("name") or "Race",
                "date": e["date"],
                "sport": guess_sport(e.get("name"), e.get("type"), e.get("description")),
                "distance_m": _distance_m(e.get("raw") or {}),
                "priority": letter,
                "start_time": None,
                "location": None,
                "links": {"intervals": str(e["id"])},
                "_primary": False,
            }
        )
    for c in out:
        if c["priority"] is None and c.pop("_primary"):
            c["priority"] = "A"  # Garmin's primary race was read as A
        c.pop("_primary", None)
    return sorted(out, key=lambda c: (c["date"], c["name"]))


def adopt(engine: Engine, *, dry_run: bool = False, origin: str = "import") -> dict[str, Any]:
    """Copy the calendars' races into VibeSynergy, once: only races it has not got yet.

    A race counts as known when one of its copies is linked already, or when VibeSynergy has
    a race on that day with the same name. Known races are left as they are.
    """
    with engine.connect() as conn:
        known = [dict(r) for r in conn.execute(select(race)).mappings()]
    linked = {
        (target, str(ref))
        for r in known
        for target, ref in (r["links"] or {}).items()
        if ref is not None
    }
    names = {(r["date"], _key(r["name"])) for r in known}
    ids = {r["id"] for r in known}
    added, skipped = [], []
    now = utc_now()
    for c in candidates(engine):
        if (
            any((t, str(ref)) in linked for t, ref in c["links"].items())
            or (
                c["date"],
                _key(c["name"]),
            )
            in names
        ):
            skipped.append({"name": c["name"], "date": c["date"].isoformat()})
            continue
        ident = base = slug(c["date"], c["name"])
        n = 2
        while ident in ids:
            ident, n = f"{base}-{n}", n + 1
        ids.add(ident)
        added.append({**c, "id": ident})
    if not dry_run and added:
        with engine.begin() as conn:
            for c in added:
                conn.execute(
                    insert(race).values(
                        subject_id=SUBJECT,
                        origin=origin,
                        created_at=now,
                        updated_at=now,
                        **c,
                    )
                )
    return {
        "added": [
            {
                "id": c["id"],
                "name": c["name"],
                "date": c["date"].isoformat(),
                "priority": c["priority"],
            }
            for c in added
        ],
        "skipped": skipped,
        "dry_run": dry_run,
    }
