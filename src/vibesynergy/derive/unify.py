"""Cross-source dedup and best-source rules.

Activities: the same session recorded by Garmin, copied to Strava, found in the Garmin
export and in an old FIT folder becomes one ``activity_unified`` row. Copies are linked by,
in order of certainty:

1. ``same_id``: the Garmin API and the Garmin export share the activityId.
2. ``fit_file``: a FIT file downloaded or exported for a Garmin activity has the same
   fit_id as an imported file.
3. ``external_id``: Strava keeps the Garmin activityId in ``external_id``
   (``garmin_ping_<id>`` / ``garmin_push_<id>``).
4. ``filename``: Strava or the account archive names an upload after the Garmin activityId
   (``<id>.fit``, ``_<id>.fit``, or a bare id in ``extra.filename`` / ``external_id``).
5. ``time``: different sources, start within 2 minutes, duration within 10% (at least
   2 minutes) and, when both have one, distance within 5% (at least 100 m); or, for copies
   recorded by different devices (a trainer app uploading to Strava while the watch records),
   time ranges overlapping by at least 90% of the longer one and distances within 15%
   (their speed models differ).

Daily values: ``daily_unified`` takes each metric from the best source that has it. The
order is set per metric prefix under ``[best_source]`` in the config, the longest matching
prefix wins, and unlisted sources come last. An order saved for one exact metric on the
Settings page (``query/best_source.py``) beats every prefix rule. Both tables are rebuilt
as a whole; that takes well under a second for a lifetime of data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, func, select, text

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import (
    activity,
    activity_link,
    activity_unified,
    daily_unified,
    fit_file,
)

# Which source's activity row is the primary one of a group.
ACTIVITY_PRIORITY = ("garmin", "garmin_export", "files", "strava", "strava_export", "withings")
_STRAVA = ("strava", "strava_export")  # the API's copy and the account archive's

DEFAULT_BEST_SOURCE: dict[str, list[str]] = {
    "": ["garmin", "garmin_export", "withings", "strava", "files"],
    "body.": ["withings", "garmin", "garmin_export"],
    "bp.": ["withings", "garmin", "garmin_export"],
    "sleep.": ["garmin", "garmin_export", "withings"],
}

START_TOLERANCE = timedelta(minutes=2)
# Two sessions can't be recorded at once: this much overlap means the same session.
MIN_OVERLAP = 0.9
OVERLAP_WINDOW = timedelta(minutes=15)  # the widest start gap the overlap rule looks at
OVERLAP_DISTANCE = 0.15  # devices estimate distance differently (e.g. a trainer app vs a watch)
_GARMIN_EXTERNAL = re.compile(r"garmin_(?:ping|push)_(\d+)")
# Whole upload name only; one Garmin activityId (docs/SOURCES.md).
_GARMIN_ID_IN_UPLOAD_NAME = re.compile(
    r"^(?:(\d+)|(\d+)\.(?:fit|gpx|tcx)(?:\.gz)?|_(\d+)\.fit)$",
    re.IGNORECASE,
)
_FIELDS = (
    "start_utc",
    "local_date",
    "tz_offset_s",
    "sport",
    "name",
    "elapsed_s",
    "moving_s",
    "distance_m",
    "ascent_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "norm_power",
    "avg_speed",
    "calories",
    "training_load",
)


@dataclass(frozen=True)
class UnifyStats:
    activities: int
    groups: int
    daily_values: int


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


def _garmin_id_from_upload_name(value: str) -> str | None:
    if not value or not str(value).strip():
        return None
    match = _GARMIN_ID_IN_UPLOAD_NAME.match(str(value).strip())
    if not match:
        return None
    return next(g for g in match.groups() if g is not None)


def _rank(source: str) -> int:
    return ACTIVITY_PRIORITY.index(source) if source in ACTIVITY_PRIORITY else 99


def _activity_fingerprint(conn) -> tuple[int, object]:
    count, updated = conn.execute(
        select(func.count(), func.max(activity.c.updated_at)).select_from(activity)
    ).one()
    return int(count or 0), updated


def _link_phase(engine: Engine, conn=None) -> tuple[list[dict], list[dict], list[dict], dict, dict]:
    from vibesynergy.query import coaching, session_purpose

    def read(bound):
        name_rules: list[dict] = []  # kept in the return shape; coaching follows the purpose
        purpose_overrides = session_purpose._overrides(bound)
        rows = [dict(r._mapping) for r in bound.execute(select(activity))]
        fits = bound.execute(
            select(fit_file.c.fit_id, fit_file.c.source_activity_id).where(
                fit_file.c.source_activity_id.is_not(None)
            )
        ).all()
        before = _activity_fingerprint(bound)
        return rows, fits, name_rules, purpose_overrides, before

    if conn is None:
        with engine.connect() as opened:
            rows, fits, name_rules, purpose_overrides, before = read(opened)
    else:
        rows, fits, name_rules, purpose_overrides, before = read(conn)
    groups, rules = link_activities(rows, fits)
    now = utc_now()
    by_key = {_key(r["source"], r["source_id"]): r for r in rows}
    garmin_fit = {str(aid): fid for fid, aid in fits}
    link_rows, unified_rows = [], []
    for members in groups.values():
        members.sort(key=lambda k: (_rank(by_key[k]["source"]), k))
        primary = members[0]
        for key in members:
            r = by_key[key]
            rule = "primary" if key == primary else rules.get(key, "time")
            link_rows.append(
                {
                    "source": r["source"],
                    "source_id": r["source_id"],
                    "group_id": primary,
                    "rule": rule,
                }
            )
        unified = _unified(primary, [by_key[k] for k in members], garmin_fit, now)
        unified["purpose"] = session_purpose.effective(
            unified["sport"], unified["name"], unified["sources"], purpose_overrides
        )
        unified["kind"] = coaching.kind(unified["purpose"])
        unified_rows.append(unified)
    return rows, link_rows, unified_rows, name_rules, before


def _relink_under_lock(engine: Engine, conn, snapshot: tuple, *, bound: int = 3) -> tuple:
    """While the write lock is held, rebuild links if the activity table still moved.

    The last attempt takes its fingerprint on this connection, so the rows written
    afterwards are the snapshot just read, not an earlier one.
    """
    rows, link_rows, unified_rows, rules, before = snapshot
    for _ in range(bound):
        if _activity_fingerprint(conn) == before:
            return rows, link_rows, unified_rows, rules, before
        rows, link_rows, unified_rows, rules, before = _link_phase(engine, conn)
    if _activity_fingerprint(conn) != before:
        rows, link_rows, unified_rows, rules, before = _link_phase(engine, conn)
    return rows, link_rows, unified_rows, rules, before


def rebuild(
    engine: Engine,
    best_source: Mapping[str, Sequence[str]] | None = None,
    *,
    exact: Mapping[str, Sequence[str]] | None = None,
) -> UnifyStats:
    max_attempts = 3
    rows, link_rows, unified_rows, rules, before = _link_phase(engine)
    for _ in range(max_attempts - 1):
        with engine.connect() as conn:
            after = _activity_fingerprint(conn)
        if after == before:
            break
        rows, link_rows, unified_rows, rules, before = _link_phase(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        rows, link_rows, unified_rows, rules, before = _relink_under_lock(
            engine,
            conn,
            (rows, link_rows, unified_rows, rules, before),
        )
        groups = len({row["group_id"] for row in link_rows})
        conn.execute(delete(activity_link))
        conn.execute(delete(activity_unified))
        for batch in _batches(link_rows, 500):
            conn.execute(activity_link.insert(), batch)
        for batch in _batches(unified_rows, 200):
            conn.execute(activity_unified.insert(), batch)
        daily = _rebuild_daily(conn, {**DEFAULT_BEST_SOURCE, **(best_source or {})}, exact or {})
        conn.commit()
    return UnifyStats(len(rows), groups, daily)


def link_activities(
    rows: list[dict], fits: Sequence[tuple[str, str]]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Group activity keys; returns ({group root: [keys]}, {key: rule that linked it}).

    Identifier rules link unconditionally. Time matching is stricter, because it is only a
    guess: candidate pairs are tried closest start first, and two groups are merged only if
    they share no source and every activity in one matches every activity in the other.
    Without that, one activity lacking a distance could chain unrelated sessions together.
    """
    uf = _UnionFind()
    rules: dict[str, str] = {}
    by_key = {_key(r["source"], r["source_id"]): r for r in rows}
    members: dict[str, list[dict]] = {key: [r] for key, r in by_key.items()}

    def merge(a: str, b: str, rule: str) -> None:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            return
        uf.union(a, b)
        root = uf.find(a)
        members[root] = members.pop(ra, []) + members.pop(rb, [])
        rules.setdefault(a, rule)
        rules.setdefault(b, rule)

    def link(a: str, b: str, rule: str) -> None:
        if a in by_key and b in by_key:
            merge(a, b, rule)

    fit_to_garmin = {fid: str(aid) for fid, aid in fits}
    for key, r in by_key.items():
        uf.find(key)
        if r["source"] == "garmin_export":
            link(key, _key("garmin", r["source_id"]), "same_id")
        if r["source"] == "strava_export":
            link(key, _key("strava", r["source_id"]), "same_id")
        if r["source"] == "files" and r["source_id"] in fit_to_garmin:
            garmin_id = fit_to_garmin[r["source_id"]]
            link(key, _key("garmin", garmin_id), "fit_file")
            link(key, _key("garmin_export", garmin_id), "fit_file")
        if r["source"] == "strava":
            extra = r.get("extra") or {}
            external = str(extra.get("external_id") or "")
            if match := _GARMIN_EXTERNAL.search(external):
                link(key, _key("garmin", match.group(1)), "external_id")
                link(key, _key("garmin_export", match.group(1)), "external_id")
        if r["source"] in _STRAVA:
            extra = r.get("extra") or {}
            upload = extra.get("filename") or extra.get("external_id") or ""
            if gid := _garmin_id_from_upload_name(str(upload)):
                link(key, _key("garmin", gid), "filename")
                link(key, _key("garmin_export", gid), "filename")

    ordered = sorted(rows, key=lambda r: r["start_utc"])
    candidates = []
    for i, a in enumerate(ordered):
        # A trainer app's copy can start well into a longer ride (``_inside``): look as far
        # as that ride lasts, not only the overlap window.
        reach = max(OVERLAP_WINDOW, timedelta(seconds=a["elapsed_s"] or a["moving_s"] or 0))
        for b in ordered[i + 1 :]:
            gap = b["start_utc"] - a["start_utc"]
            if gap > reach:
                break
            if a["source"] != b["source"] and _matches(a, b):
                candidates.append(
                    (gap, _key(a["source"], a["source_id"]), _key(b["source"], b["source_id"]))
                )
    for _, a, b in sorted(candidates):
        ga, gb = members[uf.find(a)], members[uf.find(b)]
        if ga is gb:
            continue
        # Groups that share a source are two sessions, unless that source holds the same
        # session twice: a trainer app's copy inside the watch's (both uploaded to Strava).
        shared = {m["source"] for m in ga} & {m["source"] for m in gb}
        if shared and not all(_inside(x, y) for x in ga for y in gb if x["source"] == y["source"]):
            continue
        if all(_matches(x, y) for x in ga for y in gb):
            merge(a, b, "time")

    groups: dict[str, list[str]] = {}
    for key in by_key:
        groups.setdefault(uf.find(key), []).append(key)
    return groups, rules


_INDOOR = ("virtual", "indoor", "trainer")
_FAMILIES = (("ride", ("ride", "cycl", "bik")), ("run", ("run",)), ("swim", ("swim",)))


def _indoor(r: dict) -> bool:
    """A trainer or app session (Zwift, MyWhoosh): its distance is virtual, not comparable."""
    return any(word in str(r.get("sport") or "").lower() for word in _INDOOR)


def _family(r: dict) -> str | None:
    text = str(r.get("sport") or "").lower()
    return next((name for name, words in _FAMILIES if any(w in text for w in words)), None)


def _span(r: dict) -> tuple[datetime, datetime] | None:
    duration = r["elapsed_s"] or r["moving_s"]
    if not duration:
        return None
    return r["start_utc"], r["start_utc"] + timedelta(seconds=duration)


def _inside(a: dict, b: dict) -> bool:
    """An indoor recording that lies within another session of the same sport is part of it:
    a trainer app (MyWhoosh, Zwift) uploads its own copy of part of a ride the watch recorded
    whole, sometimes to the same Strava account. Both must be the same family and one indoor."""
    fam = _family(a)
    if fam is None or fam != _family(b) or not (_indoor(a) or _indoor(b)):
        return False
    sa, sb = _span(a), _span(b)
    if sa is None or sb is None:
        return False
    overlap = (min(sa[1], sb[1]) - max(sa[0], sb[0])).total_seconds()
    shorter = min((sa[1] - sa[0]).total_seconds(), (sb[1] - sb[0]).total_seconds())
    return overlap >= MIN_OVERLAP * shorter


def _matches(a: dict, b: dict) -> bool:
    close_start = abs(a["start_utc"] - b["start_utc"]) <= START_TOLERANCE
    return (close_start and _same_session(a, b)) or _overlapping(a, b) or _inside(a, b)


def _overlapping(a: dict, b: dict) -> bool:
    da = a["elapsed_s"] or a["moving_s"]
    db = b["elapsed_s"] or b["moving_s"]
    if not da or not db:
        return False
    end_a = a["start_utc"] + timedelta(seconds=da)
    end_b = b["start_utc"] + timedelta(seconds=db)
    overlap = (min(end_a, end_b) - max(a["start_utc"], b["start_utc"])).total_seconds()
    if overlap < MIN_OVERLAP * max(da, db):
        return False
    if _indoor(a) or _indoor(b):
        return _family(a) == _family(b)  # virtual distance: the time says it
    ma, mb = a["distance_m"], b["distance_m"]
    return not (ma and mb and abs(ma - mb) > max(100.0, OVERLAP_DISTANCE * max(ma, mb)))


def _same_session(a: dict, b: dict) -> bool:
    da = a["elapsed_s"] or a["moving_s"]
    db = b["elapsed_s"] or b["moving_s"]
    if da and db and abs(da - db) > max(120.0, 0.1 * max(da, db)):
        return False
    if _indoor(a) or _indoor(b):
        return _family(a) == _family(b)  # virtual distance: the time says it
    ma, mb = a["distance_m"], b["distance_m"]
    return not (ma and mb and abs(ma - mb) > max(100.0, 0.05 * max(ma, mb)))


def _unified(primary: str, members: list[dict], garmin_fit: dict[str, str], now) -> dict:
    row = {field: None for field in _FIELDS}
    for member in members:  # primary first; others only fill gaps
        for field in _FIELDS:
            if row[field] is None and member[field] is not None:
                row[field] = member[field]
    strava = next((m for m in members if m["source"] in _STRAVA and m["name"]), None)
    others = [m["name"] for m in members if m["source"] not in _STRAVA and m["name"]]
    if strava and (not others or strava["name"] not in others):
        row["name"] = strava["name"]  # edited on Strava: the user's title wins
    stream_key = None
    for m in members:  # original FIT files beat Strava streams
        fit = m["fit_id"] or (
            garmin_fit.get(m["source_id"]) if m["source"] in ("garmin", "garmin_export") else None
        )
        if fit and not fit.startswith("strava-"):
            stream_key = fit
            break
    if stream_key is None:  # then the archive's original file, then the API's streams
        keys = [m["fit_id"] for m in members if m["fit_id"]]
        stream_key = next((k for k in keys if k.startswith("strava-export-")), None) or next(
            iter(keys), None
        )
    return {
        **row,
        "group_id": primary,
        "subject_id": members[0]["subject_id"],
        "stream_key": stream_key,
        "primary_source": members[0]["source"],
        "sources": [_key(m["source"], m["source_id"]) for m in members],
        "updated_at": now,
    }


def source_order(
    metric: str,
    best_source: Mapping[str, Sequence[str]] | None = None,
    exact: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """The sources to prefer for ``metric``, best first: an exact rule, else the longest
    matching prefix."""
    if exact and exact.get(metric):
        return list(exact[metric])
    rules = {**DEFAULT_BEST_SOURCE, **(best_source or {})}
    prefix = max((p for p in rules if metric.startswith(p)), key=len, default="")
    return list(rules.get(prefix, []))


def _rebuild_daily(
    conn,
    rules: Mapping[str, Sequence[str]],
    exact: Mapping[str, Sequence[str]] | None = None,
) -> int:
    """daily_unified = the best-ranked source per (day, metric), in one SQL statement."""
    cases, params = [], {}
    # An exact metric rule comes first; its unlisted sources rank after its listed ones.
    for i, (metric, sources) in enumerate(sorted((exact or {}).items())):
        if not sources:
            continue
        params[f"e{i}"] = metric
        for rank, source in enumerate(sources):
            params[f"e{i}_{rank}"] = source
            cases.append(f"WHEN metric = :e{i} AND source = :e{i}_{rank} THEN {rank}")
        cases.append(f"WHEN metric = :e{i} THEN 1000")
    # Longest prefix first, so "body." beats the default "". Within a prefix, unlisted
    # sources rank after listed ones (1000) instead of falling through to a shorter prefix.
    for i, (prefix, sources) in enumerate(sorted(rules.items(), key=lambda kv: -len(kv[0]))):
        params[f"p{i}"] = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        match = f"metric LIKE :p{i} ESCAPE '\\'"
        for rank, source in enumerate(sources):
            params[f"s{i}_{rank}"] = source
            cases.append(f"WHEN {match} AND source = :s{i}_{rank} THEN {rank}")
        cases.append(f"WHEN {match} THEN 1000")
    rank_sql = f"CASE {' '.join(cases)} ELSE 1000 END"
    conn.execute(delete(daily_unified))
    conn.execute(
        text(
            f"""
            INSERT INTO daily_unified (subject_id, date, metric, value, unit, source)
            SELECT subject_id, date, metric, value, unit, source FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY subject_id, date, metric ORDER BY {rank_sql}, source
                ) AS rn
                FROM daily_metric
            ) WHERE rn = 1
            """
        ),
        params,
    )
    return conn.execute(text("SELECT count(*) FROM daily_unified")).scalar_one()


def _batches(rows: list, size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]
