"""Read-only audit of cross-source activity linking (docs/SOURCES.md)."""

from __future__ import annotations

from sqlalchemy import Engine, func, select

from vibesynergy.derive import unify
from vibesynergy.store.schema import activity, activity_link, activity_unified

_STRAVA = frozenset({"strava", "strava_export"})
_GARMIN = frozenset({"garmin", "garmin_export"})


def activity_quality(engine: Engine, *, limit: int = 200) -> dict:
    """Summarise linking completeness; never raises."""
    try:
        return _audit(engine, limit=limit)
    except Exception:
        return {
            "activities": 0,
            "unified": 0,
            "groups_with_strava": 0,
            "groups_with_garmin": 0,
            "same_source_groups": [],
            "unlinked_candidates": [],
            "suspect_double_counts": [],
        }


def _audit(engine: Engine, *, limit: int) -> dict:
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(activity))]
        links = [dict(r._mapping) for r in conn.execute(select(activity_link))]
        unified_n = conn.execute(select(func.count()).select_from(activity_unified)).scalar_one()

    by_key = {unify._key(r["source"], r["source_id"]): r for r in rows}
    group_of: dict[str, str] = {
        unify._key(link["source"], link["source_id"]): link["group_id"] for link in links
    }
    by_group: dict[str, list[str]] = {}
    for key, gid in group_of.items():
        by_group.setdefault(gid, []).append(key)

    same_source_groups = []
    groups_with_strava = 0
    groups_with_garmin = 0
    for gid, keys in by_group.items():
        sources = [by_key[k]["source"] for k in keys if k in by_key]
        if any(s in _STRAVA for s in sources):
            groups_with_strava += 1
        if any(s in _GARMIN for s in sources):
            groups_with_garmin += 1
        seen: set[str] = set()
        dupes = []
        for s in sources:
            if s in seen:
                dupes.append(s)
            seen.add(s)
        if dupes:
            same_source_groups.append({"group_id": gid, "sources": sorted(set(dupes))})
    same_source_groups = same_source_groups[:limit]

    unlinked = _unlinked_candidates(rows, group_of, by_key, limit)
    suspect = _suspect_double_counts(by_group, by_key, unlinked, limit)

    return {
        "activities": len(rows),
        "unified": int(unified_n or 0),
        "groups_with_strava": groups_with_strava,
        "groups_with_garmin": groups_with_garmin,
        "same_source_groups": same_source_groups,
        "unlinked_candidates": unlinked,
        "suspect_double_counts": suspect,
    }


def _unlinked_candidates(
    rows: list[dict],
    group_of: dict[str, str],
    by_key: dict[str, dict],
    limit: int,
) -> list[dict]:
    out: list[dict] = []
    ordered = sorted(rows, key=lambda r: r["start_utc"])
    for i, a in enumerate(ordered):
        ka = unify._key(a["source"], a["source_id"])
        for b in ordered[i + 1 :]:
            gap = b["start_utc"] - a["start_utc"]
            if gap > unify.OVERLAP_WINDOW:
                break
            if a["source"] == b["source"]:
                continue
            if not unify._matches(a, b):
                continue
            kb = unify._key(b["source"], b["source_id"])
            if group_of.get(ka) and group_of.get(kb) and group_of[ka] == group_of[kb]:
                continue
            out.append(_candidate_pair(ka, kb, a, b))
            if len(out) >= limit:
                return out
    return out


def _candidate_pair(ka: str, kb: str, a: dict, b: dict) -> dict:
    start_gap_s = abs((a["start_utc"] - b["start_utc"]).total_seconds())
    da = a["elapsed_s"] or a["moving_s"]
    db = b["elapsed_s"] or b["moving_s"]
    duration_gap_pct = None
    if da and db:
        duration_gap_pct = abs(da - db) / max(da, db)
    ma, mb = a["distance_m"], b["distance_m"]
    distance_gap_pct = None
    if ma and mb:
        distance_gap_pct = abs(ma - mb) / max(ma, mb)
    return {
        "a": ka,
        "b": kb,
        "start_gap_s": start_gap_s,
        "duration_gap_pct": duration_gap_pct,
        "distance_gap_pct": distance_gap_pct,
        "sport": a["sport"] if a["sport"] == b["sport"] else f"{a['sport']}/{b['sport']}",
    }


def _suspect_double_counts(
    by_group: dict[str, list[str]],
    by_key: dict[str, dict],
    unlinked: list[dict],
    limit: int,
) -> list[dict]:
    unlinked_by_key: dict[str, list[dict]] = {}
    for row in unlinked:
        unlinked_by_key.setdefault(row["a"], []).append(row)
        unlinked_by_key.setdefault(row["b"], []).append(row)

    out: list[dict] = []
    for gid, keys in by_group.items():
        sources = {by_key[k]["source"] for k in keys if k in by_key}
        if not (sources & _STRAVA and sources & _GARMIN):
            continue
        hits = []
        for key in keys:
            for cand in unlinked_by_key.get(key, []):
                other = cand["b"] if cand["a"] == key else cand["a"]
                if other not in keys:
                    hits.append(cand)
        if hits:
            out.append({"group_id": gid, "members": keys, "unlinked_candidates": hits[:10]})
        if len(out) >= limit:
            break
    return out
