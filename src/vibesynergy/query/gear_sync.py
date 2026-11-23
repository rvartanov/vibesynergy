"""Gear of a published workout kept the same in VibeSynergy, on Strava and on Garmin (#114,
phase 5).

**One switch, one watch.** The gear follows the switch of the texts: Settings → Sync between
sources → "Keep name, texts and gear the same on Strava and Garmin" (``[publication]
text_sync_enabled``). It is checked in the same three-way check as the name and public text
(``text_sync._sync_one`` calls ``sync``), on the same watch and with the same Strava read: the
activity Strava returns holds its ``gear_id``. While the switch is off nothing here runs.

**What each platform can hold.**

- *Strava*: one gear item per activity, a bike or a pair of shoes (``gear_id`` on the
  activity update; ``"none"`` clears it). A Strava item is its own Strava slot. A main item of
  your own that is not on Strava leaves Strava as it is.
- *Garmin*: any number of gear items per activity (link and unlink by gear uuid). Only the
  Garmin items you matched (``gear_map``) take part: a Garmin item you did not match is never
  added, removed or taken in.
- Gear without a slot on either (a pull buoy, paddles, fins) stays in VibeSynergy only. It
  is not put in the public text either: #112 defines the "Gear:" line for a planned
  session's notes, not for a published workout.

**Three-way compare per platform**, as the texts: VibeSynergy's gear now, the gear
VibeSynergy last wrote to (or agreed with on) that platform (``text_sync.synced["gear"]``)
and the platform's gear now:

- platform = last-written: no outside change; VibeSynergy's gear is pushed when it differs;
- platform ≠ last-written: an outside change; it is taken into VibeSynergy (history source
  ``strava`` or ``garmin``) and then pushed to the other platform;
- both changed: the latest change wins, the other is kept in the history as "overwritten".
  The time of an outside change is when the normal sync stored that copy, else the check
  that found it; on a tie the platform wins, and Strava over Garmin.

**First sync** (no last-written gear yet): VibeSynergy's gear wins; when VibeSynergy has no
gear at all, Strava's (else Garmin's) is taken in.

**Guard (#161).** Right before a write the platform's gear is read again; when it changed
since the read the decision was made on, nothing is written and the whole check runs again
(``text_sync.Changed``). Every write is read back. Gear Strava refuses (a bike on a run) is
remembered and not pushed again until the gear changes; the workout page says so, and the
texts keep syncing.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from vibesynergy.core.secrets import SecretStore
from vibesynergy.query import gear_map, text_sync, workout_gear
from vibesynergy.store.schema import activity, activity_link

log = logging.getLogger(__name__)

PLATFORMS = text_sync.PLATFORMS
FIELD = "gear"  # the key in text_sync.synced, and the field text_sync.touch is told about
_RANK = {"strava": 2, "garmin": 1, "vs": 0}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def enabled(rt) -> bool:
    """The gear follows the text sync's switch: one setting for both (#114)."""
    return text_sync.enabled(rt)


def touch(engine, group: str, now: datetime | None = None) -> None:
    """The gear changed in VibeSynergy: the workout's next check comes after the wait."""
    text_sync.touch(engine, group, [FIELD], now)


# --- the gear each platform should have --------------------------------------------------


def want(state: dict[str, Any], garmin: dict[str, str]) -> dict[str, Any]:
    """What VibeSynergy's gear means per platform: Strava's one id ("none" for no gear,
    None: leave Strava as it is) and Garmin's matched uuids."""
    main, how = state.get("main"), state.get("how")
    slot = gear_map.strava_slot(main)
    if slot:
        strava: str | None = slot
    elif main is None and how == "own":
        strava = "none"  # you said: no gear
    else:
        strava = None  # gear of your own without a Strava slot, or nothing known
    return {
        "strava": strava,
        "garmin": sorted({garmin[r] for r in workout_gear.refs_of(state) if r in garmin}),
    }


def _from_platform(
    platform: str,
    value: Any,
    state: dict[str, Any],
    items: dict[str, dict[str, Any]],
    garmin: dict[str, str],
    family: str,
) -> dict[str, Any]:
    """VibeSynergy's gear after taking in what ``platform`` has (``main`` as for
    ``workout_gear.write``: a ref, "none", or None to keep Strava's or the default)."""
    keep_main = state["main"] if state["how"] == "own" else None
    if state["how"] == "own" and state["main"] is None:
        keep_main = workout_gear.NO_GEAR
    extras = list(state["extras"])
    if platform == "strava":
        if state["main"] and not gear_map.strava_slot(state["main"]):
            main = keep_main  # your own main item has no Strava slot: Strava cannot change it
        else:
            main = workout_gear.NO_GEAR if value == "none" else f"strava:{value}"
        return {"main": main, "extras": extras}
    back = {uuid: ref for ref, uuid in garmin.items()}
    refs = [back[u] for u in value if u in back]
    mains = [
        r
        for r in refs
        if items.get(r, {}).get("kind") in gear_map.MAIN_KINDS or gear_map.strava_slot(r)
    ]
    if mains:
        fit = workout_gear._MAIN_FOR.get(family)
        mains.sort(key=lambda r: (items.get(r, {}).get("kind") != fit, r))
        main = mains[0]
    elif state["main"] and state["main"] in garmin:
        main = workout_gear.NO_GEAR  # its matched Garmin item was taken off on Garmin
    else:
        main = keep_main
    mapped = set(garmin)
    extras = sorted(
        {e for e in extras if e not in mapped}
        | {r for r in refs if r not in mains and items.get(r, {}).get("source") == "vs"}
    )
    return {"main": main, "extras": extras}


def _refs_for(platform: str, value: Any, garmin: dict[str, str]) -> list[str]:
    """A platform's gear as VibeSynergy refs (for the history of an overwritten change)."""
    if platform == "strava":
        return [] if value in (None, "none") else [f"strava:{value}"]
    back = {uuid: ref for ref, uuid in garmin.items()}
    return sorted(back[u] for u in value if u in back)


def _stored(rt, found: dict[str, str], managed: set[str]) -> dict[str, tuple[Any, datetime]]:
    """The gear of the copies the normal sync stored, and when they were stored."""
    out: dict[str, tuple[Any, datetime]] = {}
    with rt.engine.connect() as conn:
        for platform, source_id in found.items():
            row = conn.execute(
                select(activity.c.extra, activity.c.updated_at).where(
                    activity.c.source == platform, activity.c.source_id == source_id
                )
            ).first()
            at = text_sync._aware(row.updated_at) if row else None
            if row is None or at is None:
                continue
            extra = row.extra if isinstance(row.extra, dict) else {}
            if platform == "strava":
                out[platform] = (str(extra.get("gear_id") or "none"), at)
            else:
                ids = extra.get("gear_ids")
                ids = ids if isinstance(ids, list) else []
                out[platform] = (sorted({str(i) for i in ids if i} & managed), at)
    return out


# --- reading and writing one platform ----------------------------------------------------


def _read(client, platform: str, source_id: str, managed: set[str]) -> Any:
    value = client.read_gear(source_id)
    if platform == "strava":
        return str(value or "none")
    return sorted(set(value or []) & managed)


class Refused(Exception):
    """The platform did not take the gear (for example a bike on a run)."""


def _write(
    rt, platform: str, source_id: str, client, before: Any, target: Any, managed: set[str]
) -> Any:
    """Guarded write of the gear to one platform; returns what it holds after."""
    from vibesynergy.query import publication

    lock = SecretStore(rt.settings.secrets_dir, "publication")
    with lock.lock(f"{platform}-{source_id}"):
        fresh = _read(client, platform, source_id, managed)  # the #161 guard
        if fresh != before:
            raise text_sync.Changed(f"the gear on {platform} changed since it was read")
        try:
            if platform == "strava":
                client.write_gear(source_id, target)
            else:
                for uuid in sorted(set(target) - set(fresh)):
                    client.add_gear(source_id, uuid)
                for uuid in sorted(set(fresh) - set(target)):
                    client.remove_gear(source_id, uuid)
        except Exception as exc:
            from vibesynergy.sources.strava.client import StravaError

            if isinstance(exc, StravaError) and 400 <= exc.status < 500 and exc.status != 429:
                raise Refused(f"Strava did not take the gear: {exc}") from exc
            raise
        after = _read(client, platform, source_id, managed)
        if after != target:
            raise publication.Conflict(f"{platform} did not return the synced gear")
        return after


# --- the gear part of one text-sync check ------------------------------------------------


def sync(
    rt,
    group: str,
    found: dict[str, str],
    live: dict[str, Any],
    details: dict[str, dict],
    synced: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """The three-way check of the workout's gear, inside ``text_sync._sync_one``. Keeps its
    last-written gear in ``synced["gear"]`` (stored at once). Returns what it did."""
    engine = rt.engine
    garmin = gear_map.garmin_of(engine)
    managed = set(garmin.values())
    remote: dict[str, Any] = {}
    if "strava" in found:
        remote["strava"] = str(details["strava"].get("gear_id") or "none")
    if "garmin" in found:
        remote["garmin"] = _read(live["garmin"], "garmin", found["garmin"], managed)
    mine = dict(synced.get(FIELD) or {})
    state = workout_gear.current(engine, group)
    vs_refs = workout_gear.refs_of(state)
    base = mine.get("base")
    stored = _stored(rt, found, managed)
    items = {i["ref"]: i for i in gear_map.catalog(engine, rt.raw, date.today())}
    family = workout_gear._family(workout_gear._rows(engine).get(group, {}).get("sport"))
    result: dict[str, Any] = {}

    candidates: list[tuple[datetime, int, str, Any]] = []
    first = []
    for platform in found:
        last = mine.get(platform)
        if last is None:
            first.append(platform)
            continue
        if remote[platform] == last:
            continue  # no outside change (VibeSynergy's own write reads back as this)
        copy = stored.get(platform)
        at = min(copy[1], now) if copy and copy[0] == remote[platform] else now
        candidates.append((at, _RANK[platform], platform, remote[platform]))
    if candidates and base is not None and vs_refs != base:
        when = text_sync._aware(workout_gear.last_vs_change(engine, group)) or _EPOCH
        candidates.append((when, _RANK["vs"], "vs", vs_refs))
    if candidates:
        winner = max(candidates, key=lambda c: (c[0], c[1]))
        if winner[2] != "vs":
            new = _from_platform(winner[2], winner[3], state, items, garmin, family)
            workout_gear.write(engine, group, new["main"], new["extras"])
            state = workout_gear.current(engine, group)
        new_refs = workout_gear.refs_of(state)
        for _at, _rank, source, value in candidates:
            refs = value if source == "vs" else _refs_for(source, value, garmin)
            if source != winner[2] and refs != new_refs:
                workout_gear.record(engine, group, refs, None, source=source, kind="overwritten")
                result.setdefault("overwritten", []).append(source)
        before = base if base is not None else vs_refs
        if winner[2] != "vs" and new_refs != before:
            workout_gear.record(
                engine, group, new_refs, before, source=winner[2], kind="taken", author="sync"
            )
            result["taken_from"] = winner[2]
    elif first and not vs_refs and state["how"] is None:
        # First sync and VibeSynergy has no gear: take Strava's, else Garmin's.
        for platform in PLATFORMS:
            value = remote.get(platform)
            if platform in first and value not in (None, "none", []):
                new = _from_platform(platform, value, state, items, garmin, family)
                workout_gear.write(engine, group, new["main"], new["extras"])
                state = workout_gear.current(engine, group)
                if workout_gear.refs_of(state) != vs_refs:
                    workout_gear.record(
                        engine,
                        group,
                        workout_gear.refs_of(state),
                        vs_refs,
                        source=platform,
                        kind="taken",
                        author="sync",
                    )
                    result["taken_from"] = platform
                break
    target = want(state, garmin)
    mine["base"] = workout_gear.refs_of(state)
    refused = dict(mine.get("refused") or {})
    pushes = {}
    for platform in found:
        goal = target[platform]
        if goal is None or remote[platform] == goal:
            mine[platform] = remote[platform]  # agreed: no echo of it later
            refused.pop(platform, None)
        elif refused.get(platform, {}).get("goal") == goal:
            continue  # refused before: not again until the gear changes
        else:
            pushes[platform] = goal
    mine["refused"] = refused

    def save() -> None:
        synced[FIELD] = dict(mine)
        text_sync._update(engine, group, synced=synced)

    save()
    written = []
    for platform, goal in pushes.items():
        try:
            mine[platform] = _write(
                rt, platform, found[platform], live[platform], remote[platform], goal, managed
            )
        except Refused as exc:
            refused[platform] = {"goal": goal, "error": str(exc)[:300], "at": now.isoformat()}
            mine["refused"] = refused
            save()
            result["refused"] = platform
            continue
        refused.pop(platform, None)
        save()
        written.append(platform)
    if written:
        result["written"] = sorted(written)
    return result


def page_state(rt, group: str) -> dict[str, Any]:
    """For the workout page: is the gear synced, and did a platform refuse it."""
    on = enabled(rt)
    published = False
    problem = None
    if on:
        with rt.engine.connect() as conn:
            refs = [
                (s, i)
                for s, i in conn.execute(
                    select(activity_link.c.source, activity_link.c.source_id).where(
                        activity_link.c.group_id == group
                    )
                )
                if s in PLATFORMS
            ]
            published = bool(text_sync._published_groups(conn, refs))
        row = text_sync._row(rt.engine, group)
        refused = ((row or {}).get("synced") or {}).get(FIELD, {}).get("refused") or {}
        problem = next((r.get("error") for r in refused.values() if r.get("error")), None)
    return {
        "enabled": on,
        "published": published,
        "state": "refused" if problem else ("active" if published else None),
        "problem": problem,
    }
