"""Plan horizon guard: is every planned session of the coming days on Garmin?

Deterministic and read-only. A plan workout (intervals.icu) counts as on Garmin when the
calendar merge in ``plan.plan`` folded at least one Garmin copy into it (intervals.icu's
push, VibeSynergy's own strength workout, a brick's multisport copy). Two copies matched by
name are fine for strength and pilates (VibeSynergy's emoji copy next to intervals.icu's); for
other sports they are a duplicate.

The window is ``DAYS``: intervals.icu pushes about six days ahead (today + 5). It grows to
9 to 14 days when VibeSynergy delivers every sport itself.

Two more checks:
- content: a workout VibeSynergy delivers itself must carry the plan's current content (the
  delivery fingerprint equals the plan's). A plan change (intervals.icu's ``updated``) older than an
  hour that is still not on Garmin is ``outdated`` (the delivery job runs every few minutes).
- fill: the coach keeps the plan filled up to the last day of the window. Single rest
  days are normal, so the plan is ``behind`` when the last planned workout is two or more
  days before the window's end.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.query import plan as plan_q
from vibesynergy.store.schema import garmin_strength_delivery, icu_event

DAYS = 6
# intervals.icu pushes about six days ahead; sessions it still delivers (indoor rides for
# Zwift, sports not switched to VibeSynergy) are checked only that far.
PUSH_DAYS = 6
# How long a plan change may wait for VibeSynergy's delivery job before it counts as outdated.
DELIVERY_GRACE = timedelta(hours=1)


# A session planned as a choice ("Vrijdag optie: rust, pilates of zwemmen", "optioneel",
# "keuzedag") need not be on the watch: it is no problem when it is missing.
_OPTIONAL = re.compile(r"\b(optie|optioneel|keuzedag|optional|option)\b", re.IGNORECASE)


def optional(title: str | None) -> bool:
    return bool(_OPTIONAL.search(title or ""))


def status(
    engine: Any,
    today: date,
    days: int = DAYS,
    vibesynergy_sports: tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    end = today + timedelta(days=days - 1)
    calendar = plan_q.plan(engine, Window(today, end), today, include_sync_status=False)
    missing: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    checked = 0
    for item in calendar["items"]:
        if item.get("source") != plan_q.INTERVALS or item.get("kind") not in plan_q.TRAINING_KINDS:
            continue
        day = item["date"] if isinstance(item["date"], date) else date.fromisoformat(item["date"])
        if not today <= day <= end:
            continue
        own = _vibesynergy_delivers(item, vibesynergy_sports)
        if not own and day > today + timedelta(days=PUSH_DAYS - 1):
            continue
        checked += 1
        copies = item.get("garmin_copies") or []
        entry = {"date": day.isoformat(), "title": item.get("title")}
        if not copies:
            if not optional(item.get("title")):
                missing.append(entry)
        elif (
            sum(1 for c in copies if c.get("match") == "name") > 1
            and not own  # VibeSynergy's emoji copy next to intervals.icu's is on purpose
        ):
            duplicates.append(entry)
    outdated = _outdated(engine, today, end, vibesynergy_sports, now or utc_now())
    fill = _fill(engine, today, end)
    level = "ok" if not (missing or duplicates or outdated or fill["behind"]) else "warn"
    return {
        "level": level,
        "from": today.isoformat(),
        "through": end.isoformat(),
        "checked": checked,
        "missing": missing,
        "duplicates": duplicates,
        "outdated": outdated,
        "fill": fill,
        "not_in_plan": [
            {"date": str(i.get("date")), "title": i.get("title")}
            for i in calendar.get("not_in_plan") or []
        ],
    }


def _vibesynergy_delivers(item: dict[str, Any], sports: tuple[str, ...]) -> bool:
    """VibeSynergy itself puts this session on Garmin: strength and pilates always, endurance
    sports when switched on (indoor rides stay with intervals.icu for Zwift)."""
    family = plan_q.family(item.get("sport"))
    if family == "strength":
        return True
    sport = (item.get("sport") or "").lower()
    if family == "ride" and "virtual" in sport:
        return False
    return {"run": "run", "ride": "ride", "swim": "swim"}.get(family or "") in sports


def _outdated(
    engine: Any, today: date, end: date, sports: tuple[str, ...], now: datetime
) -> list[dict[str, Any]]:
    """Workouts VibeSynergy delivers whose Garmin copy lacks the plan's current content."""
    if engine is None:
        return []
    from vibesynergy.query import garmin_endurance, garmin_strength

    lookahead = (end - today).days
    wanted = garmin_strength.desired(engine, today, lookahead)
    wanted.update(garmin_endurance.desired(engine, today, sports, lookahead))
    if not wanted:
        return []
    with engine.connect() as conn:
        rows = {
            r.icu_event_id: r
            for r in conn.execute(
                select(garmin_strength_delivery).where(
                    garmin_strength_delivery.c.icu_event_id.in_(list(wanted))
                )
            )
        }
        changed = {
            r.id: r.updated or r.fetched_at
            for r in conn.execute(
                select(icu_event.c.id, icu_event.c.updated, icu_event.c.fetched_at).where(
                    icu_event.c.id.in_(
                        [p for w in wanted.values() for p in (w.get("parts") or [w["event_id"]])]
                    )
                )
            )
        }
    out = []
    for event_id, want in wanted.items():
        row = rows.get(event_id)
        if row is not None and row.state == "delivered" and row.fingerprint == want["fingerprint"]:
            continue
        parts = want.get("parts") or [event_id]  # a brick changes with either part
        stamps = [changed[p] for p in parts if changed.get(p) is not None]
        since = max(stamps) if stamps else None
        if since is not None and now - since < DELIVERY_GRACE:
            continue
        day = want["date"]
        out.append(
            {
                "date": day.isoformat() if isinstance(day, date) else str(day),
                "title": want["name"],
                "error": row.last_error if row is not None else None,
            }
        )
    return sorted(out, key=lambda e: e["date"])


def _fill(engine: Any, today: date, end: date) -> dict[str, Any]:
    """Is the plan filled up to the window's last day (one rest day at the end is fine)?"""
    if engine is None:
        return {"planned_through": None, "behind": False}
    with engine.connect() as conn:
        last = conn.execute(
            select(func.max(icu_event.c.date)).where(
                icu_event.c.category == "WORKOUT",
                icu_event.c.date >= today,
                icu_event.c.date <= end,
            )
        ).scalar()
    if isinstance(last, str):
        last = date.fromisoformat(last)
    behind = last is None or (end - last).days >= 2
    return {"planned_through": last.isoformat() if last else None, "behind": behind}


def for_runtime(rt: Any, day: date) -> dict[str, Any] | None:
    """The guard with the athlete's own horizon and delivered sports (portal, coach)."""
    from vibesynergy.portal import prefs

    try:
        stored = prefs.load(rt.settings.data_dir)
        return status(
            rt.engine,
            day,
            prefs.horizon_days(rt.settings.data_dir),
            tuple(stored.get("garmin_delivery") or ()),
        )
    except Exception:  # a page or brief must still answer
        return None


def _own_copy(title: str | None) -> bool:
    """VibeSynergy's copies carried a sport emoji in front during the parallel week."""
    first = (title or "").strip()[:1]
    return bool(first) and not first.isalnum() and ord(first) > 0x2000


def _delivered(engine: Any) -> set[str]:
    """Garmin calendar ids (``workout:<scheduled id>``) VibeSynergy delivered itself. Since 7-11
    its copies carry no emoji, so the delivery ledger is what says a copy is ours."""
    from sqlalchemy import select

    from vibesynergy.store.schema import garmin_strength_delivery as d

    with engine.connect() as conn:
        rows = conn.execute(select(d.c.scheduled_id).where(d.c.scheduled_id.is_not(None)))
        return {f"workout:{row[0]}" for row in rows}


def compare(engine: Any, today: date, days: int = DAYS) -> list[dict[str, Any]]:
    """Per planned session in the window: is intervals.icu's copy on the Garmin, is
    VibeSynergy's, and what else sits there (the parallel week before switching the
    intervals.icu push off per sport)."""
    end = today + timedelta(days=days - 1)
    calendar = plan_q.plan(engine, Window(today, end), today, include_sync_status=False)
    ours = _delivered(engine)
    rows = []
    for item in calendar["items"]:
        if item.get("source") != plan_q.INTERVALS or item.get("kind") not in plan_q.TRAINING_KINDS:
            continue
        title = (item.get("title") or "").strip()
        found = item.get("garmin_copies") or []
        copies = [c.get("title") or "" for c in found]
        own = [
            c.get("title") or ""
            for c in found
            if str(c.get("source_id")) in ours or _own_copy(c.get("title"))
        ]
        theirs = [c for c in copies if c not in own and c.strip()[:12] == title[:12]]
        rows.append(
            {
                "date": str(item["date"]),
                "sport": item.get("sport"),
                "title": title,
                "intervals": bool(theirs),
                "vibesynergy": bool(own),
                "other": [c for c in copies if c not in own and c not in theirs],
            }
        )
    return rows
