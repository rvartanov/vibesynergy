"""Looking inside and across activities: intervals (laps as work and recovery), comparing
activities side by side, gear with its mileage (from Strava, which counts per bike and shoe),
and the Garmin strength-exercise catalog your synced sessions actually use."""

from __future__ import annotations

import hashlib
import statistics
from datetime import date, timedelta
from importlib.metadata import version
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import views
from vibesynergy.query.units import presentation
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.raw import RawStore
from vibesynergy.store.schema import (
    activity,
    activity_gear,
    activity_unified,
    gear_default,
    gear_service,
    gear_setting,
)

STEADY_SPREAD = 0.15  # laps within 15% of each other: a steady session, not intervals
SHOE_REPLACE_KM = (600, 800)  # the usual range to replace running shoes
COMPARE_FIELDS = (
    "local_date",
    "sport",
    "name",
    "moving_s",
    "elapsed_s",
    "distance_m",
    "ascent_m",
    "avg_speed",
    "avg_hr",
    "max_hr",
    "avg_power",
    "norm_power",
    "training_load",
    "calories",
)


# Intervals --------------------------------------------------------------------------------


def intervals(engine: Engine, parquet: ParquetStore, activity_id: str) -> dict[str, Any]:
    detail = views.activity_detail(engine, parquet, activity_id)
    laps = []
    for number, lap in enumerate(detail["laps"], start=1):
        seconds = lap.get("moving_s") or lap.get("elapsed_s") or 0
        distance = lap.get("distance_m") or 0
        speed = lap.get("avg_speed") or (distance / seconds if seconds and distance else None)
        laps.append(
            {
                "lap": number,  # sources count laps from 0 or from 1
                "seconds": round(seconds, 1),
                "distance_m": round(distance, 1) if distance else None,
                "speed": round(speed, 3) if speed else None,
                "avg_hr": lap.get("avg_hr"),
                "avg_power": lap.get("avg_power"),
                "sport_units": presentation(
                    detail["activity"]["sport"],
                    {"avg_speed": speed, "avg_power": lap.get("avg_power")},
                ),
            }
        )
    basis = "avg_power" if sum(1 for lap in laps if lap["avg_power"]) >= len(laps) / 2 else "speed"
    values = [lap[basis] for lap in laps if lap[basis]]
    if len(values) < 2:
        return {"activity": detail["activity"]["group_id"], "laps": laps, "basis": None}
    median, top = statistics.median(values), max(values)
    steady = (top - min(values)) / median < STEADY_SPREAD if median else True
    threshold = (median + top) / 2
    for lap in laps:
        v = lap[basis]
        lap["kind"] = "steady" if steady else "work" if v and v >= threshold else "recovery"
    work = [lap for lap in laps if lap["kind"] == "work"]
    rest = [lap for lap in laps if lap["kind"] == "recovery"]
    return {
        "activity": detail["activity"]["group_id"],
        "sport": detail["activity"]["sport"],
        "basis": "power" if basis == "avg_power" else "speed",
        "structure": "steady" if steady else f"{len(work)} work intervals",
        "laps": laps,
        "work": _summary(work, basis, detail["activity"]["sport"]),
        "recovery": _summary(rest, basis, detail["activity"]["sport"]),
        "fade_pct": _fade(work, basis),
    }


def _summary(laps: list[dict], basis: str, sport: str) -> dict[str, Any] | None:
    if not laps:
        return None
    seconds = sum(lap["seconds"] for lap in laps)

    def weighted(key: str) -> float | None:
        pairs = [(lap[key], lap["seconds"]) for lap in laps if lap[key]]
        total = sum(s for _, s in pairs)
        return round(sum(v * s for v, s in pairs) / total, 2) if total else None

    result = {
        "count": len(laps),
        "seconds": round(seconds, 1),
        "avg_speed": weighted("speed"),
        "avg_hr": weighted("avg_hr"),
        "avg_power": weighted("avg_power"),
    }
    result["sport_units"] = presentation(sport, result)
    return result


def _fade(work: list[dict], basis: str) -> float | None:
    """Last work interval against the first, in percent (negative: slower / weaker)."""
    if len(work) < 2 or not work[0][basis] or not work[-1][basis]:
        return None
    return round(100 * (work[-1][basis] - work[0][basis]) / work[0][basis], 1)


# Compare ----------------------------------------------------------------------------------


def compare(engine: Engine, parquet: ParquetStore, ids: list[str]) -> dict[str, Any]:
    if not 2 <= len(ids) <= 5:
        raise ValueError("compare 2 to 5 activities")
    rows = []
    for activity_id in ids:
        a = views.activity_detail(engine, parquet, activity_id)["activity"]
        rows.append(
            {
                "id": a["group_id"],
                **{k: a.get(k) for k in COMPARE_FIELDS},
                "sport_units": a["sport_units"],
            }
        )
    first = rows[0]
    for row in rows[1:]:
        row["vs_first"] = {
            k: round(row[k] - first[k], 3)
            for k in COMPARE_FIELDS
            if isinstance(row.get(k), int | float) and isinstance(first.get(k), int | float)
        }
    return {"activities": rows, "note": "vs_first: difference with the first activity listed."}


# Gear -------------------------------------------------------------------------------------

# Gear identity is namespaced per source ("strava:g1", not "g1") so a future second source
# can never collide with or silently merge into an existing physical-item record (V-C6).
# Garmin also collects its own gear snapshot (datasets.py "gear") and per-activity gear use
# (mappers.py "gear_ids"), but Garmin's own list is empty for the athlete and its item field names
# (brand/model/retired) are unverified against real data; only presence/freshness is reported
# here until that is checked against a real account. Activity gear is cited by source id
# (`garmin:<uuid>`) without distance or model. Reconciling the same physical item across
# sources needs an explicit link, never an automatic name/model match.
GARMIN_GEAR_DATASET = "gear"
GARMIN_ACTIVITIES_DATASET = "activities"


def _snapshot(
    raw: RawStore, source: str, dataset: str, *, fallback: bool = False
) -> tuple[dict[str, Any] | None, Any]:
    """Current raw snapshot and the archive record that holds its content hash."""
    raw_id = raw.current_id(source, dataset, "current")
    if raw_id is None and fallback:
        raw_id = next(iter(raw.current_ids(source, dataset)), None)
    if not raw_id:
        return None, None
    try:
        record = raw.load(raw_id)
    except (OSError, ValueError, AttributeError):
        return None, None
    return {
        "source": source,
        "dataset": dataset,
        "raw_item_id": record.id,
        "sha256": record.sha256,
    }, record


def _dataset_freshness(engine: Engine, source: str, dataset: str) -> dict[str, Any]:
    """Collection status for one dataset, from the shared sync-status query."""
    status = views.sync_status(engine, source, datasets={(source, dataset)})
    row = next(iter(status["datasets"]), None)
    if row is None:
        return {"source": source, "dataset": dataset, "state": "missing", "collected_at": None}
    return {
        "source": source,
        "dataset": dataset,
        "state": row["processing"]["state"],
        "collected_at": row["processing"]["last_received_at"],
    }


def _source_freshness(engine: Engine, raw: RawStore, source: str, dataset: str) -> dict[str, Any]:
    """Collection state/freshness for one raw dataset, reusing the shared sync-status query."""
    fresh = _dataset_freshness(engine, source, dataset)
    out: dict[str, Any] = {
        "state": fresh["state"],
        "collected_at": fresh["collected_at"],
        "item_count": None,
        "reference": None,
    }
    reference, record = _snapshot(raw, source, dataset)
    out["reference"] = reference
    payload = None
    if record is not None:
        try:
            payload = record.json()
        except (OSError, ValueError, AttributeError):
            payload = None  # still cite the snapshot; its fields are not readable
    if isinstance(payload, dict) and isinstance(payload.get("gear"), list):
        out["item_count"] = len(payload["gear"])
    return out


def _activity_gear(engine: Engine) -> list[dict[str, Any]]:
    """Garmin gear cited on activities, by uuid only. No distance, brand or model."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(activity.c.local_date, activity.c.extra).where(
                activity.c.source == "garmin",
                func.json_extract(activity.c.extra, "$.gear_ids").is_not(None),
            )
        ).all()
    seen: dict[str, dict[str, Any]] = {}
    for local_day, extra in rows:
        ids = (extra or {}).get("gear_ids")
        if not isinstance(ids, list):
            continue
        unique = {str(item).strip() for item in ids if item and str(item).strip()}
        for gid in unique:
            entry = seen.get(gid)
            if entry is None:
                entry = seen[gid] = {
                    "id": gid,
                    "ref": f"garmin:{gid}",
                    "source": "garmin",
                    "sessions": 0,
                    "first_seen": None,
                    "last_seen": None,
                }
            entry["sessions"] += 1
            day = str(local_day)
            entry["first_seen"] = min(entry["first_seen"] or day, day)
            entry["last_seen"] = max(entry["last_seen"] or day, day)
    return sorted(seen.values(), key=lambda e: (-e["sessions"], e["ref"]))


def gear(engine: Engine, raw: RawStore, today: date) -> dict[str, Any]:
    """Bikes and shoes from Strava with Strava's total distance, how much VibeSynergy saw them
    used (count, distance, last use), and shoe replacement warnings. Garmin's own gear
    collection is reported for freshness/reconciliation only (see module notes above)."""
    reference, record = _snapshot(raw, "strava", "gear", fallback=True)
    try:
        items = record.json() if record is not None else {}
    except (OSError, ValueError):
        items = {}  # an unreadable snapshot is still cited, but lists no gear
    if not isinstance(items, dict):
        items = {}
    gear_id = func.json_extract(activity.c.extra, "$.gear_id")
    with engine.connect() as conn:
        usage = {
            gid: (count, distance, last)
            for gid, count, distance, last in conn.execute(
                select(
                    gear_id,
                    func.count(),
                    func.sum(activity.c.distance_m),
                    func.max(activity.c.local_date),
                )
                .where(activity.c.source == "strava", gear_id.is_not(None))
                .group_by(gear_id)
            )
        }
        recent = {
            gid
            for (gid,) in conn.execute(
                select(gear_id).where(
                    activity.c.source == "strava",
                    activity.c.local_date >= today - timedelta(days=30),
                    gear_id.is_not(None),
                )
            )
        }
    settings = gear_settings(engine)
    services = gear_services(engine, today)
    adjust = _adjustments(engine)
    out = []
    for gid, g in items.items():
        if not isinstance(g, dict):
            continue
        kind = (
            "shoes" if str(gid).startswith("g") else "bike" if str(gid).startswith("b") else "other"
        )
        count, seen_m, last = usage.get(gid, (0, 0, None))
        # Your own changes per activity and the defaults per sport count at once.
        moved = adjust.get(f"strava:{gid}")
        if moved:
            count += moved["count"]
            seen_m = (seen_m or 0) + moved["m"]
            last = max(filter(None, (str(last) if last else None, moved["last"])), default=None)
        km = round(((g.get("distance") or 0) + (moved["m"] if moved else 0)) / 1000, 1)
        entry = {
            "id": gid,
            "ref": f"strava:{gid}",
            "source": "strava",
            "name": g.get("nickname") or g.get("name"),
            "brand": g.get("brand_name"),
            "model": g.get("model_name"),
            "kind": kind,
            "total_km": km,
            "retired": bool(g.get("retired")),
            "primary": bool(g.get("primary")),
            "activities_seen": count,
            "km_seen": round((seen_m or 0) / 1000, 1),
            "last_used": str(last) if last else None,
            "used_last_30_days": gid in recent
            or bool(moved and moved["last"] and moved["last"] >= str(today - timedelta(days=30))),
        }
        own = settings.get(entry["ref"], {})
        entry["status"] = own.get("status") or ("retired" if entry["retired"] else "active")
        entry["retired"] = entry["status"] == "retired"
        entry["life_km"] = own.get("life_km") or (SHOE_REPLACE_KM[1] if kind == "shoes" else None)
        entry["note"] = own.get("note")
        entry["services"] = [
            {**svc, **_service_status(svc, km, today)} for svc in services.get(entry["ref"], [])
        ]
        life = own.get("life_km")
        if entry["status"] == "active" and life:
            # Your own limit: a warning from 90% of it on.
            if km >= life:
                entry["warning"] = f"{km:.0f} km: past your limit of {life:.0f} km"
                entry["warning_level"] = "past"
            elif km >= 0.9 * life:
                entry["warning"] = f"{km:.0f} km: close to your limit of {life:.0f} km"
                entry["warning_level"] = "near"
        elif kind == "shoes" and entry["status"] == "active" and km >= SHOE_REPLACE_KM[0]:
            entry["warning"] = (
                f"{km:.0f} km: past the usual {SHOE_REPLACE_KM[0]}-{SHOE_REPLACE_KM[1]} km for "
                "running shoes"
                if km >= SHOE_REPLACE_KM[1]
                else f"{km:.0f} km: in the usual {SHOE_REPLACE_KM[0]}-{SHOE_REPLACE_KM[1]} km "
                "replacement range"
            )
            entry["warning_level"] = "past" if km >= SHOE_REPLACE_KM[1] else "near"
        out.append(entry)
    out.sort(key=lambda e: (e["retired"], e["kind"], -(e["total_km"] or 0)))
    garmin = _source_freshness(engine, raw, "garmin", GARMIN_GEAR_DATASET)
    references = [item for item in (reference, garmin["reference"]) if item]
    return {
        "gear": out,
        "defaults": gear_defaults(engine),
        "sports": _recent_sports(engine, today),
        "activity_gear": _activity_gear(engine),
        "references": references,
        "other_sources": {"garmin": garmin},
        "note": "Totals are Strava's (they include activities before VibeSynergy); 'seen' counts "
        "the Strava activities VibeSynergy has with that gear. Gear you set on an activity or as a "
        "sport's default is added to (or taken off) both. Garmin also collects its own gear "
        "list ('other_sources'); it is not merged in until its item fields are verified against "
        "a real Garmin gear list. 'activity_gear' cites Garmin gear ids used on activities, "
        "without distance or model. 'references' cites the raw snapshots those lists came from; "
        "an unchanged snapshot keeps the same content hash.",
    }


GEAR_STATUSES = ("active", "spare", "retired")


def _group_gear(engine: Engine) -> list[dict[str, Any]]:
    """Per activity: the gear its source gave it, and the gear that counts: yours
    first, then the source's, then the sport's default from its start date on."""
    gear_id = func.json_extract(activity.c.extra, "$.gear_id")
    with engine.connect() as conn:
        source_gear = {
            f"strava:{sid}": f"strava:{gid}"
            for sid, gid in conn.execute(
                select(activity.c.source_id, gear_id).where(
                    activity.c.source == "strava", gear_id.is_not(None)
                )
            )
        }
        own = {r.source_ref: r.ref for r in conn.execute(select(activity_gear))}
        defaults = {r.sport: (r.ref, r.since) for r in conn.execute(select(gear_default))}
        groups = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.sport,
                activity_unified.c.local_date,
                activity_unified.c.distance_m,
                activity_unified.c.sources,
            )
        ).all()
    out = []
    for group_id, sport, day, distance, sources in groups:
        sources = sources or []
        base = next((source_gear[s] for s in sources if s in source_gear), None)
        mine = [own[s] for s in sources if s in own]
        if mine:
            ref, how = mine[0], "own"
        elif base:
            ref, how = base, "source"
        elif sport in defaults and day >= defaults[sport][1]:
            ref, how = defaults[sport][0], "default"
        else:
            ref, how = None, None
        out.append(
            {
                "group_id": group_id,
                "sources": sources,
                "sport": sport,
                "date": str(day),
                "m": distance or 0,
                "base": base,
                "ref": ref,
                "how": how,
            }
        )
    return out


def _adjustments(engine: Engine) -> dict[str, dict[str, Any]]:
    """What your own gear choices add to (or take off) each item's totals."""
    out: dict[str, dict[str, Any]] = {}

    def add(ref: str, sign: int, row: dict[str, Any]) -> None:
        entry = out.setdefault(ref, {"count": 0, "m": 0.0, "last": None})
        entry["count"] += sign
        entry["m"] += sign * row["m"]
        if sign > 0:
            entry["last"] = max(filter(None, (entry["last"], row["date"])))

    for row in _group_gear(engine):
        if row["ref"] == row["base"]:
            continue
        if row["base"]:
            add(row["base"], -1, row)
        if row["ref"]:
            add(row["ref"], 1, row)
    return out


def activity_gear_of(
    engine: Engine, raw: RawStore, activity_id: str, today: date
) -> dict[str, Any]:
    """The gear of one activity, how it got it (source, own, default) and what you can pick."""
    group_id = views.resolve_activity(engine, activity_id)
    row = next((r for r in _group_gear(engine) if r["group_id"] == group_id), None)
    items = gear(engine, raw, today)["gear"]
    return {
        "ref": row["ref"] if row else None,
        "how": row["how"] if row else None,
        "options": [
            {"ref": g["ref"], "name": g["name"], "kind": g["kind"]}
            for g in items
            if g["status"] != "retired" or (row and g["ref"] == row["ref"])
        ],
    }


NO_GEAR = "none"


def set_activity_gear(engine: Engine, activity_id: str, ref: str | None) -> dict[str, Any]:
    """Set the gear of one activity. ``None`` goes back to the source's (or the
    default); ``"none"`` means no gear. It counts in the totals at once."""
    if ref is not None and (not isinstance(ref, str) or not ref.strip() or len(ref) > 100):
        raise ValueError("ref is a gear ref, 'none' or null")
    group_id = views.resolve_activity(engine, activity_id)
    with engine.begin() as conn:
        sources = conn.execute(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group_id)
        ).scalar_one()
        conn.execute(activity_gear.delete().where(activity_gear.c.source_ref.in_(sources)))
        if ref is not None:
            now = utc_now()
            conn.execute(
                activity_gear.insert(),
                [
                    {"source_ref": s, "ref": None if ref == NO_GEAR else ref, "updated_at": now}
                    for s in sources
                ],
            )
    return {"id": group_id, "ref": ref}


def _recent_sports(engine: Engine, today: date) -> list[str]:
    """Sports with distance in the last year: the ones a default gear makes sense for."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(activity_unified.c.sport)
                .where(
                    activity_unified.c.local_date >= today - timedelta(days=365),
                    activity_unified.c.distance_m > 0,
                )
                .group_by(activity_unified.c.sport)
                .order_by(func.count().desc())
            ).scalars()
        )


def gear_defaults(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(gear_default).order_by(gear_default.c.sport)).mappings().all()
    return [{"sport": r["sport"], "ref": r["ref"], "since": str(r["since"])} for r in rows]


def set_gear_default(engine: Engine, sport: str, ref: str | None, today: date) -> dict[str, Any]:
    """Default gear for a sport, for its activities from today on without gear.
    ``None`` removes it."""
    if not isinstance(sport, str) or not sport.strip() or len(sport) > 60:
        raise ValueError("sport is required")
    with engine.begin() as conn:
        conn.execute(gear_default.delete().where(gear_default.c.sport == sport))
        if ref:
            conn.execute(
                gear_default.insert().values(
                    sport=sport, ref=ref, since=today, updated_at=utc_now()
                )
            )
    return {"sport": sport, "ref": ref, "since": str(today) if ref else None}


def gear_services(engine: Engine, today: date) -> dict[str, list[dict[str, Any]]]:
    with engine.connect() as conn:
        rows = conn.execute(select(gear_service).order_by(gear_service.c.what)).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = {k: row[k] for k in ("id", "what", "every_km", "every_days", "last_km")}
        item["last_done"] = row["last_done"].isoformat() if row["last_done"] else None
        out.setdefault(row["ref"], []).append(item)
    return out


def _service_status(svc: dict[str, Any], km: float, today: date) -> dict[str, Any]:
    """How far a service is: by km (from the km at the last one) and/or by days. "due" at
    100%, "soon" from 90%; the larger of the two counts."""
    shares = []
    if svc.get("every_km") and svc.get("last_km") is not None:
        shares.append((km - svc["last_km"]) / svc["every_km"])
    if svc.get("every_days") and svc.get("last_done"):
        shares.append((today - date.fromisoformat(svc["last_done"])).days / svc["every_days"])
    share = max(shares) if shares else None
    state = None if share is None else "due" if share >= 1 else "soon" if share >= 0.9 else "ok"
    return {"share": round(share, 2) if share is not None else None, "state": state}


def add_service(
    engine: Engine,
    ref: str,
    what: str,
    *,
    every_km: float | None,
    every_days: int | None,
    km_now: float | None,
    today: date,
) -> str:
    """A maintenance item for one gear item, counted from now."""
    what = (what or "").strip()
    if not what or len(what) > 80:
        raise ValueError("say what the maintenance is (at most 80 characters)")
    if not every_km and not every_days:
        raise ValueError("give a distance or a number of days")
    if every_km is not None and not 0 < every_km < 100_000:
        raise ValueError("every_km must be between 0 and 100000")
    if every_days is not None and not 0 < every_days < 3660:
        raise ValueError("every_days must be between 1 and 3659")
    import secrets

    service_id = "svc_" + secrets.token_hex(6)
    with engine.begin() as conn:
        conn.execute(
            gear_service.insert().values(
                id=service_id,
                ref=ref,
                what=what,
                every_km=every_km,
                every_days=every_days,
                last_done=today,
                last_km=km_now,
                updated_at=utc_now(),
            )
        )
    return service_id


def service_done(engine: Engine, service_id: str, *, km_now: float | None, today: date) -> None:
    with engine.begin() as conn:
        found = conn.execute(
            gear_service.update()
            .where(gear_service.c.id == service_id)
            .values(last_done=today, last_km=km_now, updated_at=utc_now())
        )
    if found.rowcount != 1:
        raise ValueError("no such maintenance item")


def remove_service(engine: Engine, service_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(gear_service.delete().where(gear_service.c.id == service_id))


def gear_settings(engine: Engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(gear_setting)).mappings().all()
    return {row["ref"]: dict(row) for row in rows}


def set_gear(
    engine: Engine,
    ref: str,
    *,
    status: str | None = None,
    life_km: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Your own settings for one gear item. None clears a field; edits save as they are."""
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 100:
        raise ValueError("ref is required")
    if status is not None and status not in GEAR_STATUSES:
        raise ValueError(f"status must be one of {', '.join(GEAR_STATUSES)}")
    if life_km is not None and not 0 < life_km < 100_000:
        raise ValueError("life_km must be between 0 and 100000")
    if note is not None and len(note) > 1000:
        raise ValueError("note is at most 1000 characters")
    values = {
        "status": status,
        "life_km": life_km,
        "note": (note or "").strip() or None,
        "updated_at": utc_now(),
    }
    with engine.begin() as conn:
        found = conn.execute(select(gear_setting.c.ref).where(gear_setting.c.ref == ref)).first()
        if found:
            conn.execute(gear_setting.update().where(gear_setting.c.ref == ref).values(**values))
        else:
            conn.execute(gear_setting.insert().values(ref=ref, **values))
    return {"ref": ref, **values, "updated_at": values["updated_at"].isoformat()}


# Exercises ----------------------------------------------------------------------------------


def _humanize(code: str) -> str:
    """A readable fallback for a FIT enum value Garmin's own catalog does not name."""
    return code.replace("_", " ").strip().lower().capitalize()


def _exercise_identity(category: Any, name: Any) -> tuple[str, str | None] | None:
    """The one normalised category/variant pair used by history, coaching and validation."""
    text = str(category or "").strip()
    if not text:
        return None
    variant = str(name or "").strip()
    return text, variant or None


def _exercise_ref(category: str, name: str | None) -> str:
    return f"garmin:exercise:{category}" + (f"/{name}" if name else "")


def _index_exercise_rows(rows: list[dict[str, str]]) -> tuple[dict[tuple[str, str], str], int, str]:
    """One row per category/variant. A repeated pair (the picker 'Recent' block) is dropped."""
    labels: dict[tuple[str, str], str] = {}
    duplicates = 0
    for entry in rows:
        key = (str(entry["category"]), str(entry["exercise"]))
        if key in labels:
            duplicates += 1
            continue
        labels[key] = str(entry["name"])
    fingerprint = hashlib.sha256(
        "\n".join(f"{cat}\t{ex}\t{labels[(cat, ex)]}" for cat, ex in sorted(labels)).encode()
    ).hexdigest()
    return labels, duplicates, fingerprint


def _load_catalog() -> tuple[dict[tuple[str, str], str], dict[str, Any]]:
    """Labels plus the catalog reference. Repeated category/variant rows collapse."""
    from garminconnect.exercises import CATEGORIES, EXERCISES

    labels, duplicates, fingerprint = _index_exercise_rows(EXERCISES)
    try:
        package_version = version("garminconnect")
    except Exception:  # pragma: no cover - package metadata should always resolve
        package_version = "unknown"
    return labels, {
        "source": "garmin connect workout-editor catalog",
        "package": "garminconnect",
        "package_version": package_version,
        "categories": len(CATEGORIES),
        "known_variants": len(labels),
        "duplicate_rows": duplicates,
        "fingerprint": fingerprint,
    }


def exercise_catalog() -> dict[str, Any]:
    """The bundled Garmin workout-editor catalog as one reference.

    Display labels come from the pinned ``garminconnect`` package. Repeated
    category/variant rows collapse, so a catalog refresh cannot mint a second identity
    for the same exercise. The fingerprint covers the unique rows only.
    """
    return _load_catalog()[1]


def exercise_references(steps: list[dict]) -> dict[str, Any]:
    """Resolve proposed strength steps against the shared catalog.

    The first category/variant wins. A later copy is marked ``duplicate`` and does not
    become a second reference. This does not write a workout.
    """
    if not isinstance(steps, list):
        raise ValueError("exercise steps must be a list")
    labels, catalog = _load_catalog()
    seen: dict[tuple[str, str | None], int] = {}
    resolved: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        identity = (
            _exercise_identity(step.get("category"), step.get("name"))
            if isinstance(step, dict)
            else None
        )
        if identity is None:
            resolved.append(
                {
                    "index": index,
                    "ref": None,
                    "source": "garmin",
                    "category": step.get("category") if isinstance(step, dict) else None,
                    "name": step.get("name") if isinstance(step, dict) else None,
                    "label": None,
                    "known": False,
                    "duplicate": False,
                    "problem": "missing_category",
                }
            )
            continue
        category, variant = identity
        key = identity
        label = labels.get((category, variant or ""))
        entry = {
            "index": index,
            "ref": _exercise_ref(category, variant),
            "source": "garmin",
            "category": category,
            "name": variant,
            "label": label or _humanize(variant or category),
            "known": label is not None,
            "duplicate": key in seen,
            "problem": None,
        }
        if entry["duplicate"]:
            entry["duplicate_of"] = seen[key]
        else:
            seen[key] = index
        resolved.append(entry)
    return {
        "steps": resolved,
        "unique": len(seen),
        "unknown": sum(1 for step in resolved if not step["known"] and not step["duplicate"]),
        "catalog": catalog,
    }


def exercises(engine: Engine) -> dict[str, Any]:
    """The Garmin strength-exercise identity (category + specific variant) your synced
    sessions used, matched against Garmin's own workout-editor catalog (bundled with the
    ``garminconnect`` dependency). The same ``category``/``name`` pairs Garmin validates when
    writing a strength workout step, so coaching and workout validation refer
    to one shared identity rather than re-deriving it."""
    labels, catalog = _load_catalog()
    with engine.connect() as conn:
        rows = conn.execute(
            select(activity.c.local_date, activity.c.extra).where(
                activity.c.source == "garmin",
                func.json_extract(activity.c.extra, "$.exercises").is_not(None),
            )
        ).all()
    seen: dict[tuple[str, str | None], dict[str, Any]] = {}
    for local_day, extra in rows:
        counted: set[tuple[str, str | None]] = set()
        for item in (extra or {}).get("exercises") or []:
            key = (
                _exercise_identity(item.get("category"), item.get("name"))
                if isinstance(item, dict)
                else None
            )
            if key is None or key in counted:
                continue
            counted.add(key)
            entry = seen.get(key)
            if entry is None:
                label = labels.get((key[0], key[1] or ""))
                entry = seen[key] = {
                    "ref": _exercise_ref(key[0], key[1]),
                    "category": key[0],
                    "name": key[1],
                    "label": label or _humanize(key[1] or key[0]),
                    "known": label is not None,
                    "sessions": 0,
                    "first_seen": None,
                    "last_seen": None,
                }
            entry["sessions"] += 1
            day = str(local_day)
            entry["first_seen"] = min(entry["first_seen"] or day, day)
            entry["last_seen"] = max(entry["last_seen"] or day, day)
    catalogue = sorted(seen.values(), key=lambda e: (-e["sessions"], e["label"]))
    return {
        "exercises": catalogue,
        "catalog": catalog,
        "freshness": _dataset_freshness(engine, "garmin", GARMIN_ACTIVITIES_DATASET),
        "note": "'known' marks a category/variant Garmin's own catalog names; others are shown "
        "with Garmin's own enum turned into readable text. Counts are strength sessions synced "
        "from Garmin, not sets or weight. 'freshness' is the Garmin activities collection. "
        "'catalog.fingerprint' changes only when a unique category/variant row changes.",
    }
