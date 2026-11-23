"""The gear of one workout, on its page (#114, phase 5): one tap to link the likely gear.

**What a workout's gear is.** One *main* item, the bike or the shoes (it counts in the km
totals and is Strava's one gear slot, kept in ``activity_gear`` as before), plus any number
of *extra* items of your own: swim gear such as a pull buoy, paddles or fins, a wetsuit
(``activity_gear_extra``). Until you choose, the main item is the one Strava gave the
activity, else the sport's default gear (``workouts._group_gear``).

**The suggestion** (``suggest``) is deterministic and says why:

1. ``last``: the gear of your most recent earlier workout of the same sport (run, ride, swim,
   ...), main item and extras, leaving out retired gear;
2. ``default``: else the default gear you set for this sport;
3. ``kind``: else the bike (for a ride) or the shoes (for a run or walk) you used last,
   then the one with the most km.

**Every change is kept** in ``gear_change`` (when, from VibeSynergy, Strava or Garmin, before
and after), like the texts' history. With "Keep name, texts and gear the same on Strava and
Garmin" on, a change of a published workout is passed on by ``gear_sync`` on the text
sync's check.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import gear_map, views, workouts
from vibesynergy.store.raw import RawStore
from vibesynergy.store.schema import (
    activity_gear_extra,
    activity_unified,
    gear_change,
    gear_default,
)

SOURCES: tuple[str, ...] = ("vs", "strava", "garmin")
# edit: changed in VibeSynergy; taken: an outside change taken in (Strava or Garmin);
# overwritten: a change that lost to a later one in the sync (kept, never current).
KINDS: tuple[str, ...] = ("edit", "taken", "overwritten")
MAX_EXTRAS = 12
NO_GEAR = workouts.NO_GEAR
# Which kind of main gear a sport uses when nothing else tells (step 3 of the suggestion).
_MAIN_FOR = {"run": "shoes", "walk": "shoes", "ride": "bike"}


def _family(sport: str | None) -> str:
    from vibesynergy.query import plan

    return plan.family(sport) or str(sport or "").lower()


def _keys(engine: Engine, group: str) -> list[str]:
    from vibesynergy.query import workout_texts

    return workout_texts._group(engine, group)[1]


def _sources(engine: Engine, group: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.scalar(
                select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
            )
            or []
        )


def _rows(engine: Engine) -> dict[str, dict[str, Any]]:
    """Every workout's gear: main ref and how it got it, extras, sport family and start."""
    with engine.connect() as conn:
        starts = dict(
            conn.execute(select(activity_unified.c.group_id, activity_unified.c.start_utc)).all()
        )
    extras = workouts.group_extras(engine)
    out = {}
    for row in workouts._group_gear(engine):
        group = row["group_id"]
        out[group] = {
            **row,
            "family": _family(row["sport"]),
            "start": starts.get(group),
            "extras": extras.get(group, []),
        }
    return out


def current(engine: Engine, activity_id: str) -> dict[str, Any]:
    """The workout's gear now: ``main`` (ref or None), ``how`` (own, source, default or
    None) and ``extras`` (refs)."""
    group = views.resolve_activity(engine, activity_id)
    row = _rows(engine).get(group) or {}
    return {
        "group": group,
        "main": row.get("ref"),
        "how": row.get("how"),
        "extras": list(row.get("extras") or []),
    }


def refs_of(state: dict[str, Any]) -> list[str]:
    """The gear of a state as one list: the main item first, then the extras."""
    return ([state["main"]] if state.get("main") else []) + sorted(state.get("extras") or [])


def suggest(
    engine: Engine, raw: RawStore, activity_id: str, today: date, *, items=None
) -> dict[str, Any] | None:
    """The most likely gear for the workout and why (see the module text), or None."""
    group = views.resolve_activity(engine, activity_id)
    rows = _rows(engine)
    me = rows.get(group)
    if me is None:
        return None
    items = items if items is not None else gear_map.catalog(engine, raw, today)
    active = {i["ref"]: i for i in items if i["status"] != "retired"}
    start = me["start"]
    earlier = [
        r
        for r in rows.values()
        if r["group_id"] != group
        and r["family"] == me["family"]
        and r["start"] is not None
        and start is not None
        and r["start"] < start
    ]
    for row in sorted(earlier, key=lambda r: (r["start"], r["group_id"]), reverse=True):
        main = row["ref"] if row["ref"] in active else None
        extras = [e for e in row["extras"] if e in active]
        if main or extras:
            return {"main": main, "extras": extras, "reason": "last", "date": row["date"]}
    with engine.connect() as conn:
        default = conn.scalar(select(gear_default.c.ref).where(gear_default.c.sport == me["sport"]))
    if default in active:
        return {"main": default, "extras": [], "reason": "default", "date": None}
    kind = _MAIN_FOR.get(me["family"])
    if kind:
        fits = [i for i in active.values() if i["kind"] == kind]
        fits.sort(key=lambda i: (i["name"].casefold(), i["ref"]))
        fits.sort(key=lambda i: i["total_km"] or 0, reverse=True)
        fits.sort(key=lambda i: i["last_used"] or "", reverse=True)
        if fits:
            return {"main": fits[0]["ref"], "extras": [], "reason": "kind", "date": None}
    return None


def history(engine: Engine, activity_id: str) -> list[dict[str, Any]]:
    """Every change of the workout's gear, newest first."""
    group = views.resolve_activity(engine, activity_id)
    keys = _keys(engine, group)
    with engine.connect() as conn:
        rows = conn.execute(
            select(gear_change)
            .where(gear_change.c.group_id.in_(keys))
            .order_by(gear_change.c.id.desc())
        ).all()
    return [
        {
            "id": r.id,
            "refs": list(r.refs or []),
            "old_refs": list(r.old_refs or []) if r.old_refs is not None else None,
            "source": r.source,
            "kind": r.kind,
            "author": r.author,
            "at": r.at.isoformat() if r.at else None,
        }
        for r in rows
    ]


def last_vs_change(engine: Engine, group: str) -> datetime | None:
    """When the gear was last changed in VibeSynergy (for "the latest change wins")."""
    keys = _keys(engine, group)
    with engine.connect() as conn:
        return conn.scalar(
            select(gear_change.c.at)
            .where(
                gear_change.c.group_id.in_(keys),
                gear_change.c.source == "vs",
                gear_change.c.kind == "edit",
            )
            .order_by(gear_change.c.id.desc())
            .limit(1)
        )


def record(
    engine: Engine,
    group: str,
    refs: list[str],
    old_refs: list[str] | None,
    *,
    source: str,
    kind: str,
    author: str | None = None,
    at: datetime | None = None,
) -> None:
    if source not in SOURCES or kind not in KINDS:
        raise ValueError("unknown gear change source or kind")
    with engine.begin() as conn:
        conn.execute(
            gear_change.insert().values(
                group_id=group,
                refs=list(refs),
                old_refs=list(old_refs) if old_refs is not None else None,
                source=source,
                kind=kind,
                author=author,
                at=at or utc_now(),
            )
        )


def _check(engine: Engine, raw: RawStore, main: str | None, extras: list[str], today: date) -> None:
    items = {i["ref"]: i for i in gear_map.catalog(engine, raw, today)}
    if main not in (None, NO_GEAR):
        found = items.get(main)
        if found is None:
            raise ValueError("unknown gear item")
        if found["source"] == "vs" and found["kind"] not in gear_map.MAIN_KINDS:
            raise ValueError("swim gear and other gear are extras, not the main item")
    if not isinstance(extras, list) or len(extras) > MAX_EXTRAS:
        raise ValueError(f"at most {MAX_EXTRAS} extra items")
    for ref in extras:
        found = items.get(ref)
        if found is None or found["source"] != "vs":
            raise ValueError("an extra item is gear of your own (a Strava item is the main item)")
    if main in extras:
        raise ValueError("an item is the main item or an extra, not both")


def write(
    engine: Engine,
    group: str,
    main: str | None,
    extras: list[str],
) -> None:
    """Store the gear without history or sync (the sync's own writes go through here)."""
    workouts.set_activity_gear(engine, group, main)
    sources = _sources(engine, group)
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            activity_gear_extra.delete().where(activity_gear_extra.c.source_ref.in_(sources))
        )
        if extras:
            conn.execute(
                activity_gear_extra.insert(),
                [
                    {"source_ref": s, "ref": ref, "updated_at": now}
                    for s in sources
                    for ref in sorted(set(extras))
                ],
            )


def set_gear(
    engine: Engine,
    raw: RawStore,
    activity_id: str,
    *,
    main: str | None,
    extras: list[str],
    today: date,
    author: str | None = "portal",
) -> dict[str, Any]:
    """Link gear to the workout: ``main`` a gear ref, ``"none"`` (no main item) or None
    (back to Strava's or the sport's default); ``extras`` gear of your own. Kept in the
    history; with the text sync on, a published workout is passed on after a short wait."""
    extras = sorted(set(extras or []))
    _check(engine, raw, main, extras, today)
    before = current(engine, activity_id)
    group = before["group"]
    write(engine, group, main, extras)
    after = current(engine, group)
    changed = refs_of(before) != refs_of(after) or before["how"] != after["how"]
    if changed:
        record(
            engine, group, refs_of(after), refs_of(before), source="vs", kind="edit", author=author
        )
        from vibesynergy.query import gear_sync

        gear_sync.touch(engine, group)
    return {"changed": changed, **after}


# --- for the page -----------------------------------------------------------------------


def _where(item: dict[str, Any], garmin: dict[str, str]) -> list[str]:
    """Where a linked item can go: its Strava slot and/or its mapped Garmin item."""
    out = []
    if gear_map.strava_slot(item["ref"]):
        out.append("strava")
    if item["ref"] in garmin:
        out.append("garmin")
    return out


def page(rt, activity_id: str, today: date) -> dict[str, Any]:
    """The gear card: what is linked, the suggestion and its reason, what you can pick, the
    history, and whether the gear is synced."""
    from vibesynergy.query import gear_sync

    engine, raw = rt.engine, rt.raw
    items = gear_map.catalog(engine, raw, today)
    by_ref = {i["ref"]: i for i in items}
    garmin = gear_map.garmin_of(engine)
    state = current(engine, activity_id)
    group = state["group"]
    hint = suggest(engine, raw, group, today, items=items)
    linked = refs_of(state)
    if hint is not None and refs_of(hint) == linked:
        hint = {**hint, "linked": True}

    def describe(ref: str) -> dict[str, Any]:
        item = by_ref.get(ref) or {"ref": ref, "name": ref, "kind": "other", "source": "vs"}
        return {
            "ref": ref,
            "name": item["name"],
            "kind": item["kind"],
            "source": item["source"],
            "goes_to": _where(item, garmin),
        }

    def selectable(item: dict[str, Any]) -> bool:
        return item["status"] != "retired" or item["ref"] in linked

    return {
        "activity_id": group,
        "main": describe(state["main"]) if state["main"] else None,
        "how": state["how"],
        "extras": [describe(r) for r in state["extras"]],
        "suggestion": (
            {
                **hint,
                "main": describe(hint["main"]) if hint["main"] else None,
                "extras": [describe(r) for r in hint["extras"]],
            }
            if hint
            else None
        ),
        "options": {
            "main": [
                describe(i["ref"])
                for i in items
                if selectable(i) and (i["source"] != "vs" or i["kind"] in gear_map.MAIN_KINDS)
            ],
            "extras": [
                describe(i["ref"])
                for i in items
                if selectable(i) and i["source"] == "vs" and i["kind"] not in gear_map.MAIN_KINDS
            ],
        },
        "history": [
            {
                **h,
                "names": [describe(r)["name"] for r in h["refs"]],
                "old_names": [describe(r)["name"] for r in h["old_refs"] or []],
            }
            for h in history(engine, group)
        ],
        "sync": gear_sync.page_state(rt, group),
    }
