"""Your gear in VibeSynergy and its one-time mapping to Strava and Garmin (#114, phase 5).

**The gear list.** VibeSynergy's gear is the bikes and shoes Strava keeps (ref
``strava:<id>``, with Strava's km totals, see ``workouts.gear``) plus gear of your own
(``gear_item``, ref ``vs:<id>``): swim gear such as a pull buoy, paddles or fins, a wetsuit,
a trainer. Gear of your own has no km total.

**The mapping.** Three lists that know nothing of each other: VibeSynergy's, Strava's and
Garmin's. A Strava item *is* its own Strava slot. Garmin's list (the ``gear`` snapshot the
Garmin sync collects) is mapped once, item by item, in ``gear_map``. ``suggest`` proposes
matches by type and name; nothing is used for the sync until you save the mapping. Gear
without a slot on either platform (most swim gear) stays in VibeSynergy only.
"""

from __future__ import annotations

import re
import secrets
from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.raw import RawStore
from vibesynergy.store.schema import gear_item, gear_map

OWN_PREFIX = "vs:"
KINDS: tuple[str, ...] = ("swim", "bike", "shoes", "other")
# The kinds that take the one main slot of a workout (counted in km, Strava's one gear id).
MAIN_KINDS: tuple[str, ...] = ("bike", "shoes")
MAX_NAME = 80
# Garmin's gear types, as its gear list names them, to VibeSynergy's kinds.
_GARMIN_KIND = {"shoes": "shoes", "shoe": "shoes", "bike": "bike", "bicycle": "bike"}
_WORD = re.compile(r"[a-z0-9]+")
# A suggested match needs at least this share of words in common (or the same kind and the
# only item of that kind on both sides). A technical threshold, not a coaching value.
MATCH_SHARE = 0.5


# --- gear of your own -------------------------------------------------------------------


def own_items(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(gear_item).order_by(gear_item.c.name)).mappings().all()
    return [{"ref": r["ref"], "name": r["name"], "kind": r["kind"], "source": "vs"} for r in rows]


def add_own(engine: Engine, name: str, kind: str) -> dict[str, Any]:
    """Add gear of your own (a pull buoy, a wetsuit). Same name and kind twice: the one."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"give the gear a name (at most {MAX_NAME} characters)")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    with engine.begin() as conn:
        for row in conn.execute(select(gear_item)).mappings():
            if row["kind"] == kind and row["name"].casefold() == name.casefold():
                return {"ref": row["ref"], "name": row["name"], "kind": row["kind"]}
        ref = OWN_PREFIX + secrets.token_hex(5)
        now = utc_now()
        conn.execute(
            gear_item.insert().values(ref=ref, name=name, kind=kind, created_at=now, updated_at=now)
        )
    return {"ref": ref, "name": name, "kind": kind}


def rename_own(engine: Engine, ref: str, name: str) -> None:
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"give the gear a name (at most {MAX_NAME} characters)")
    with engine.begin() as conn:
        done = conn.execute(
            gear_item.update().where(gear_item.c.ref == ref).values(name=name, updated_at=utc_now())
        )
    if done.rowcount != 1:
        raise ValueError("no such gear of your own")


# --- Garmin's gear list -----------------------------------------------------------------


def garmin_items(raw: RawStore) -> list[dict[str, Any]]:
    """Garmin's gear list from the last Garmin sync: uuid, a readable name, kind, retired.

    Read defensively: the snapshot's item fields are Garmin's (``displayName``,
    ``customMakeModel``, ``gearMakeName``/``gearModelName``, ``gearTypeName``,
    ``gearStatusName``) and any of them may be missing."""
    from vibesynergy.query.workouts import GARMIN_GEAR_DATASET, _snapshot

    _ref, record = _snapshot(raw, "garmin", GARMIN_GEAR_DATASET, fallback=True)
    try:
        payload = record.json() if record is not None else {}
    except (OSError, ValueError, AttributeError):
        payload = {}
    items = payload.get("gear") if isinstance(payload, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("uuid"):
            continue
        uuid = str(item["uuid"])
        make_model = " ".join(
            str(item.get(k) or "").strip() for k in ("gearMakeName", "gearModelName")
        ).strip()
        name = (
            str(item.get("displayName") or "").strip()
            or str(item.get("customMakeModel") or "").strip()
            or make_model
            or uuid
        )
        kind = _GARMIN_KIND.get(str(item.get("gearTypeName") or "").strip().casefold(), "other")
        status = str(item.get("gearStatusName") or "").casefold()
        out[uuid] = {
            "uuid": uuid,
            "name": name,
            "model": make_model or str(item.get("customMakeModel") or "").strip() or None,
            "kind": kind,
            "retired": status == "retired",
        }
    return sorted(out.values(), key=lambda g: (g["retired"], g["kind"], g["name"].casefold()))


# --- matching ---------------------------------------------------------------------------


def _words(*texts: Any) -> set[str]:
    return {w for t in texts for w in _WORD.findall(str(t or "").casefold())}


def _score(vs: dict[str, Any], garmin: dict[str, Any]) -> float:
    """How well a Garmin item matches a VibeSynergy item: the share of words in common."""
    mine = _words(vs.get("name"), vs.get("brand"), vs.get("model"))
    theirs = _words(garmin.get("name"), garmin.get("model"))
    if not mine or not theirs:
        return 0.0
    return len(mine & theirs) / min(len(mine), len(theirs))


def suggest(
    items: list[dict[str, Any]], garmin: list[dict[str, Any]], taken: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Suggested Garmin matches, ``{ref: {"uuid", "reason"}}``: same kind, then most words
    in common; each Garmin item once. Deterministic: ties go by name, then ref and uuid.

    ``reason`` is ``name`` (words in common) or ``only`` (the only item of that kind on both
    sides). ``taken``: Garmin uuids already mapped, never suggested again."""
    taken = set(taken or ())
    pairs = []
    for vs in items:
        for g in garmin:
            if g["uuid"] in taken or g["retired"] or g["kind"] != vs["kind"]:
                continue
            score = _score(vs, g)
            if score >= MATCH_SHARE:
                pairs.append((-score, str(vs.get("name") or ""), vs["ref"], g["uuid"]))
    out: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for _score_neg, _name, ref, uuid in sorted(pairs):
        if ref in out or uuid in used:
            continue
        out[ref] = {"uuid": uuid, "reason": "name"}
        used.add(uuid)
    # One item of a kind left on each side: they are the same.
    for kind in MAIN_KINDS:
        left = [v for v in items if v["kind"] == kind and v["ref"] not in out]
        right = [
            g
            for g in garmin
            if g["kind"] == kind and not g["retired"] and g["uuid"] not in used | taken
        ]
        if len(left) == 1 and len(right) == 1:
            out[left[0]["ref"]] = {"uuid": right[0]["uuid"], "reason": "only"}
            used.add(right[0]["uuid"])
    return out


# --- the mapping ------------------------------------------------------------------------


def saved(engine: Engine) -> dict[str, str | None]:
    """The saved mapping: ref -> Garmin uuid (None: you said it is not on Garmin)."""
    with engine.connect() as conn:
        return {r.ref: r.garmin_uuid for r in conn.execute(select(gear_map))}


def strava_slot(ref: str | None) -> str | None:
    """The Strava gear id of a VibeSynergy item: a Strava item is its own slot."""
    if ref and ref.startswith("strava:"):
        return ref.split(":", 1)[1]
    return None


def catalog(engine: Engine, raw: RawStore, today: date) -> list[dict[str, Any]]:
    """Every VibeSynergy gear item (Strava's and your own), with status and kind."""
    from vibesynergy.query import workouts

    return [
        {
            "ref": g["ref"],
            "name": g.get("name") or g["ref"],
            "brand": g.get("brand"),
            "model": g.get("model"),
            "kind": g["kind"],
            "source": g["source"],
            "status": g.get("status") or "active",
            "last_used": g.get("last_used"),
            "total_km": g.get("total_km"),
        }
        for g in workouts.gear(engine, raw, today)["gear"]
    ]


def mapping(engine: Engine, raw: RawStore, today: date) -> dict[str, Any]:
    """The mapping screen: per VibeSynergy item its Strava slot, its Garmin item (saved, or
    a suggestion until you save) and Garmin's list to choose from."""
    items = [i for i in catalog(engine, raw, today) if i["status"] != "retired"]
    garmin = garmin_items(raw)
    known = saved(engine)
    taken = {u for u in known.values() if u}
    hints = suggest([i for i in items if i["ref"] not in known], garmin, taken)
    rows = []
    for item in items:
        hint = hints.get(item["ref"])
        rows.append(
            {
                "ref": item["ref"],
                "name": item["name"],
                "kind": item["kind"],
                "source": item["source"],
                "strava": strava_slot(item["ref"]),
                "garmin": known.get(item["ref"]),
                "saved": item["ref"] in known,
                "suggestion": hint["uuid"] if hint else None,
                "reason": hint["reason"] if hint else None,
            }
        )
    return {
        "items": rows,
        "garmin": [g for g in garmin if not g["retired"] or g["uuid"] in taken],
        "garmin_collected": bool(garmin),
        "suggested": sum(1 for r in rows if r["suggestion"]),
        "to_map": sum(1 for r in rows if not r["saved"]),
    }


def save(engine: Engine, choices: dict[str, str | None]) -> dict[str, str | None]:
    """Save the mapping for the refs given: a Garmin uuid, or None for "not on Garmin". A
    Garmin item belongs to one VibeSynergy item at a time."""
    if not isinstance(choices, dict) or len(choices) > 500:
        raise ValueError("choices maps gear refs to a Garmin uuid or null")
    for ref, uuid in choices.items():
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 100:
            raise ValueError("a gear ref is required")
        if uuid is not None and (not isinstance(uuid, str) or not uuid.strip() or len(uuid) > 100):
            raise ValueError("a Garmin gear uuid or null")
    used = [u for u in choices.values() if u]
    if len(used) != len(set(used)):
        raise ValueError("a Garmin item can be mapped to one of your items only")
    now = utc_now()
    with engine.begin() as conn:
        if used:
            # The Garmin item moves: the item it was mapped to is no longer mapped.
            conn.execute(
                gear_map.update()
                .where(gear_map.c.garmin_uuid.in_(used), gear_map.c.ref.not_in(list(choices)))
                .values(garmin_uuid=None, updated_at=now)
            )
        for ref, uuid in choices.items():
            conn.execute(gear_map.delete().where(gear_map.c.ref == ref))
            conn.execute(gear_map.insert().values(ref=ref, garmin_uuid=uuid, updated_at=now))
    return saved(engine)


def accept_suggestions(engine: Engine, raw: RawStore, today: date) -> dict[str, str | None]:
    """Save every suggested match at once ("Use the suggestions")."""
    found = mapping(engine, raw, today)
    return save(engine, {r["ref"]: r["suggestion"] for r in found["items"] if r["suggestion"]})


def garmin_of(engine: Engine) -> dict[str, str]:
    """ref -> Garmin uuid, for the saved matches only."""
    return {ref: uuid for ref, uuid in saved(engine).items() if uuid}
