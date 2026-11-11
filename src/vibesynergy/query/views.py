"""Viewing queries behind the MCP tools and (later) the portal (docs/MCP.md §3.2 to §3.11).

Each function takes the engine (and the Parquet store where streams are involved) and
returns plain JSON-ready dicts. Values are SI; units are listed per field or metric.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.compute as pc
from sqlalchemy import Engine, and_, case, desc, func, select, tuple_

from vibesynergy.core.config import RETIRED_SOURCES
from vibesynergy.core.timeutil import Window
from vibesynergy.derive.unify import DEFAULT_BEST_SOURCE, source_order
from vibesynergy.query import evidence, journal
from vibesynergy.query import series as qs
from vibesynergy.query.catalog import KNOWN
from vibesynergy.query.units import family, pace_unit, presentation
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import (
    activity,
    activity_lap,
    activity_link,
    activity_unified,
    activity_zone_time,
    body_measurement,
    daily_metric,
    daily_unified,
    dataset_state,
    raw_item,
    sleep_session,
    sleep_stage,
    zone_set,
)

ACTIVITY_FIELDS = (
    "group_id",
    "subject_id",
    "local_date",
    "start_utc",
    "sport",
    "name",
    "kind",
    "moving_s",
    "elapsed_s",
    "distance_m",
    "ascent_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "norm_power",
    "avg_speed",
    "calories",
    "training_load",
    "sources",
)
SORTS = {
    "date": "start_utc",
    "distance": "distance_m",
    "duration": "moving_s",
    "load": "training_load",
    "np": "norm_power",
    "avg_hr": "avg_hr",
    "ascent": "ascent_m",
}
DAY_BASELINE_METRICS = (
    "hrv.night_avg",
    "hr.resting",
    "sleep.score",
    "recovery.readiness",
    "body.weight",
)


def _rows(engine: Engine, query) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [_jsonable(dict(r._mapping)) for r in conn.execute(query)]


def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


# Days ---------------------------------------------------------------------------------------


def day(engine: Engine, when: date) -> dict[str, Any]:
    from vibesynergy.query import plan as plan_q

    metrics = _rows(
        engine,
        select(
            daily_unified.c.metric,
            daily_unified.c.value,
            daily_unified.c.unit,
            daily_unified.c.source,
        )
        .where(daily_unified.c.date == when)
        .order_by(daily_unified.c.metric),
    )
    by_domain: dict[str, dict[str, Any]] = {}
    for m in metrics:
        domain = m["metric"].split(".", 1)[0]
        by_domain.setdefault(domain, {})[m["metric"]] = {
            "value": m["value"],
            "unit": m["unit"],
            "source": m["source"],
        }
    baselines = {
        metric: b
        for metric in DAY_BASELINE_METRICS
        if (b := qs.baseline(engine, metric, when)) is not None
    }
    return {
        "date": str(when),
        **plan_q.race_phase(engine, when),
        "metrics": by_domain,
        "sleep": _best_sleep(engine, when),
        "activities": activities(engine, Window(when, when), limit=50)["activities"],
        "body_measurements": _rows(
            engine,
            select(
                body_measurement.c.metric,
                body_measurement.c.value,
                body_measurement.c.unit,
                body_measurement.c.measured_at,
                body_measurement.c.source,
            )
            .where(body_measurement.c.measured_at.between(_utc_day(when), _utc_day(when, 1)))
            .order_by(body_measurement.c.measured_at),
        ),
        "vs_baseline_60d": baselines,
        "journal": journal.day_journal(engine, when),
    }


def calendar(engine: Engine, window: Window, metrics: list[str]) -> dict[str, Any]:
    result = qs.series(engine, metrics, window, granularity="day", max_points=10_000)
    return {"columns": result["columns"], "rows": result["rows"]}


def _utc_day(d: date, plus: int = 0) -> datetime:
    """Midnight UTC of ``d + plus`` days (measurement times are stored in UTC)."""
    return datetime.combine(d + timedelta(days=plus), time(), tzinfo=UTC)


# Sleep, recovery, body ----------------------------------------------------------------------


def _sleep_sources() -> list[str]:
    return DEFAULT_BEST_SOURCE["sleep."]


def _best_sleep(engine: Engine, when: date) -> dict[str, Any] | None:
    sessions = _rows(engine, select(sleep_session).where(sleep_session.c.local_date == when))
    if not sessions:
        return None
    order = _sleep_sources()
    sessions.sort(key=lambda s: order.index(s["source"]) if s["source"] in order else 99)
    best = sessions[0]
    best.pop("raw_item_id", None)
    best.pop("updated_at", None)
    best["other_sources"] = [s["source"] for s in sessions[1:]]
    return best


def sleep(
    engine: Engine, window: Window, *, stages: bool = False, timezone: str | None = None
) -> dict[str, Any]:
    nights = []
    for d in window.days():
        night = _best_sleep(engine, d)
        if night is None:
            continue
        night.pop("extra", None)
        if stages:
            night["stages"] = _rows(
                engine,
                select(sleep_stage.c.start_utc, sleep_stage.c.end_utc, sleep_stage.c.stage)
                .where(
                    sleep_stage.c.source == night["source"],
                    sleep_stage.c.session_source_id.in_(
                        [night["source_id"], f"night-{d.isoformat()}"]
                    ),
                )
                .order_by(sleep_stage.c.start_utc),
            )
        nights.append(night)
    summary = qs.stats(engine, ["sleep.score", "sleep.duration", "sleep.avg_hrv"], window)
    out: dict[str, Any] = {"nights": nights, "summary": summary}
    if timezone:
        out["regularity"] = sleep_regularity(nights, timezone)
    return out


def sleep_regularity(nights: list[dict[str, Any]], timezone: str) -> dict[str, Any] | None:
    """Average bedtime, wake time and midpoint (local clock) and how much they vary (standard
    deviation in minutes). Clock times are counted from noon, so 23:30 and 00:30 are an hour
    apart, not 23 hours."""
    zone = ZoneInfo(timezone)

    def minutes_from_noon(value: Any) -> float:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        local = moment.astimezone(zone)
        return (local.hour * 60 + local.minute - 12 * 60) % (24 * 60)

    def clock(minutes: float) -> str:
        total = round(minutes + 12 * 60) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"

    times = [
        (minutes_from_noon(n["start_utc"]), minutes_from_noon(n["end_utc"]))
        for n in nights
        if n.get("start_utc") and n.get("end_utc")
    ]
    if len(times) < 2:
        return None
    beds = [b for b, _ in times]
    wakes = [w for _, w in times]
    mids = [(b + w) / 2 if w >= b else (b + w + 24 * 60) / 2 for b, w in times]

    def summary(values: list[float]) -> dict[str, Any]:
        return {"mean": clock(statistics.fmean(values)), "sd_min": round(statistics.stdev(values))}

    return {
        "nights": len(times),
        "bedtime": summary(beds),
        "wake": summary(wakes),
        "midpoint": summary(mids),
    }


def recovery(engine: Engine, window: Window) -> dict[str, Any]:
    metrics = [
        "hrv.night_avg",
        "hrv.baseline_low",
        "hrv.baseline_high",
        "hr.resting",
        "recovery.readiness",
        "recovery.body_battery.max",
        "recovery.body_battery.min",
        "recovery.stress.avg",
        "sleep.score",
        "activity.load",
    ]
    return qs.series(engine, metrics, window, granularity="day", max_points=10_000)


def body(engine: Engine, window: Window, metrics: list[str]) -> dict[str, Any]:
    start, end = _utc_day(window.start, -1), _utc_day(window.end, 2)
    measurements = _rows(
        engine,
        select(
            body_measurement.c.measured_at,
            body_measurement.c.metric,
            body_measurement.c.value,
            body_measurement.c.unit,
            body_measurement.c.source,
        )
        .where(
            body_measurement.c.metric.in_(metrics),
            body_measurement.c.measured_at.between(start, end),
        )
        .order_by(body_measurement.c.measured_at),
    )
    trend = qs.series(engine, metrics, window, granularity=qs.auto_granularity(window))
    return {
        "measurements": measurements,
        "trend": trend,
        "stats": qs.stats(engine, metrics, window),
    }


# Activities ---------------------------------------------------------------------------------


def activities(
    engine: Engine,
    window: Window,
    *,
    sport: str | None = None,
    text: str | None = None,
    min_distance_m: float | None = None,
    min_duration_s: float | None = None,
    source: str | None = None,
    sort: str = "date",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    t = activity_unified
    where = [t.c.local_date.between(window.start, window.end)]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    if text:
        where.append(t.c.name.ilike(f"%{text}%"))
    if min_distance_m:
        where.append(t.c.distance_m >= min_distance_m)
    if min_duration_s:
        where.append(t.c.moving_s >= min_duration_s)
    if source:
        where.append(func.json_extract(t.c.sources, "$").like(f'%"{source}:%'))
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {', '.join(SORTS)}")
    column = t.c[SORTS[sort]]
    rows = _rows(
        engine,
        select(*[t.c[f] for f in ACTIVITY_FIELDS])
        .where(and_(*where))
        .order_by(desc(column).nulls_last(), desc(t.c.start_utc))
        .limit(limit)
        .offset(offset),
    )
    with engine.connect() as conn:
        for row in rows:
            weight_kg = None
            if family(row.get("sport")) == "cycle" and row.get("avg_power"):
                weight_kg = conn.scalar(
                    select(body_measurement.c.value)
                    .where(
                        body_measurement.c.metric == "body.weight",
                        body_measurement.c.subject_id == row["subject_id"],
                        body_measurement.c.measured_at <= datetime.fromisoformat(row["start_utc"]),
                    )
                    .order_by(body_measurement.c.measured_at.desc())
                    .limit(1)
                )
            row["sport_units"] = presentation(row.get("sport"), row, weight_kg=weight_kg)
            row.pop("subject_id")
    with engine.connect() as conn:
        totals = conn.execute(
            select(
                func.count(),
                func.sum(qs.activity_column("moving_s")),
                func.sum(t.c.distance_m),
                func.sum(t.c.ascent_m),
                func.sum(t.c.training_load),
            ).where(and_(*where))
        ).one()
    # A multisport is a holder with its parts: the rows say which is which, and the
    # totals count the parts, not the holder as well.
    from vibesynergy.query import multisport

    with engine.connect() as conn:
        spans = conn.execute(
            select(
                t.c.group_id,
                t.c.sport,
                t.c.start_utc,
                t.c.elapsed_s,
                qs.activity_column("moving_s"),
                t.c.distance_m,
                t.c.ascent_m,
                t.c.training_load,
            ).where(and_(*where))
        ).all()
    holders = multisport.parts(
        [
            {"id": r[0], "sport": r[1], "start_utc": r[2], "elapsed_s": r[3], "moving_s": r[4]}
            for r in spans
        ]
    )
    part_of = {part: holder for holder, ids in holders.items() for part in ids}
    for row in rows:
        gid = row.get("group_id")
        if gid in holders:
            row["parts"] = holders[gid]
        elif gid in part_of:
            row["part_of"] = part_of[gid]
    held = [r for r in spans if r[0] in holders]
    rows_total = int(totals[0])  # pagination is over rows, holders included
    total = rows_total - len(held)

    def minus(value, index):
        if value is None:
            return None
        return value - sum(r[index] or 0 for r in held)

    return {
        "activities": rows,
        "totals": {
            "count": total,
            "moving_s": minus(totals[1], 4),
            "distance_m": minus(totals[2], 5),
            "ascent_m": minus(totals[3], 6),
            "training_load": minus(totals[4], 7),
        },
        "next_offset": offset + limit if offset + limit < rows_total else None,
    }


def sports(engine: Engine) -> list[dict[str, Any]]:
    """Sports that occur in the unified activities, most frequent first."""
    t = activity_unified
    return _rows(
        engine,
        select(t.c.sport, func.count().label("count"))
        .where(t.c.sport.is_not(None))
        .group_by(t.c.sport)
        .order_by(desc("count")),
    )


def resolve_activity(engine: Engine, activity_id: str) -> str:
    """Accept a group id or any member ``source:source_id``; return the group id."""
    if ":" not in activity_id:
        raise ValueError("activity ids look like 'garmin:123' or 'strava:456'")
    source, source_id = activity_id.split(":", 1)
    with engine.connect() as conn:
        group = conn.execute(
            select(activity_link.c.group_id).where(
                activity_link.c.source == source, activity_link.c.source_id == source_id
            )
        ).scalar()
    if group is None:
        raise ValueError(f"no activity {activity_id!r} (see vibesynergy_activities)")
    return group


def select_laps_and_zones(sources, fetch_laps, fetch_zones):
    """First source that has laps, and the first source for each zone kind.

    ``fetch_laps`` / ``fetch_zones`` take ``(source, source_id)`` and return rows in
    lap-index order and kind/zone order. One loop so a bulk preload cannot drift.
    """
    laps: list[dict] = []
    zones: list[dict] = []
    zone_kinds: set[str] = set()
    for key in sources:
        source, source_id = key.split(":", 1)
        if not laps:
            laps = fetch_laps(source, source_id)
        source_zones = fetch_zones(source, source_id)
        zones.extend(zone for zone in source_zones if zone["kind"] not in zone_kinds)
        # Keep one complete set per kind; do not mix a source's partial boundaries with
        # another source's zone numbering.
        zone_kinds.update(zone["kind"] for zone in zones)
    return laps, zones


_LAP_DROP = ("raw_item_id", "updated_at", "source", "activity_source_id")


def lap_zone_index(conn, idents: set[tuple[str, str]] | None = None) -> tuple[dict, dict]:
    """Lap and zone rows keyed by source identity, in the same order as activity detail.

    ``idents`` limits the read to those source activities. The full table is only for callers
    that have not chosen a set yet.
    """
    if idents is not None and not idents:
        return {}, {}
    lap_query = select(activity_lap)
    zone_query = select(
        activity_zone_time.c.source,
        activity_zone_time.c.activity_source_id,
        activity_zone_time.c.updated_at,
        activity_zone_time.c.kind,
        activity_zone_time.c.zone,
        activity_zone_time.c.seconds,
        activity_zone_time.c.low,
    )
    if idents:
        wanted = tuple(idents)
        lap_query = lap_query.where(
            tuple_(activity_lap.c.source, activity_lap.c.activity_source_id).in_(wanted)
        )
        zone_query = zone_query.where(
            tuple_(activity_zone_time.c.source, activity_zone_time.c.activity_source_id).in_(wanted)
        )
    laps_by: dict[tuple, list] = {}
    for row in conn.execute(lap_query).mappings():
        raw = dict(row)
        key = (raw["source"], raw["activity_source_id"])
        item = _jsonable(raw)
        for field in _LAP_DROP:
            item.pop(field, None)
        laps_by.setdefault(key, []).append(item)
    for bucket in laps_by.values():
        bucket.sort(key=lambda lap: lap["lap_index"])
    zones_by: dict[tuple, list] = {}
    for row in conn.execute(zone_query).mappings():
        raw = dict(row)
        key = (raw["source"], raw["activity_source_id"])
        zones_by.setdefault(key, []).append(_jsonable(raw))
    for bucket in zones_by.values():
        bucket.sort(key=lambda zone: (zone["kind"], zone["zone"]))
    return laps_by, zones_by


def digest_detail(row: dict, laps_by: dict, zones_by: dict, parquet: ParquetStore) -> dict:
    """Recorded facts for one unified row.

    Laps, zones and ``facts_version`` match ``activity_detail``. Sport units and stream
    charts are display-only and are left out; the input version does not use them.
    """

    def fetch_laps(source, source_id):
        return laps_by.get((source, source_id), [])

    def fetch_zones(source, source_id):
        return zones_by.get((source, source_id), [])

    laps, zones = select_laps_and_zones(row["sources"], fetch_laps, fetch_zones)
    activity = _jsonable(
        {
            key: row.get(key)
            for key in (
                *evidence.FIELDS,
                "sources",
                "stream_key",
                "group_id",
                "primary_source",
            )
        }
    )
    detail = {"activity": activity, "laps": [dict(lap) for lap in laps], "zones": list(zones)}
    detail["evidence"] = evidence.activity_version(detail, parquet)
    return detail


def activity_detail(engine: Engine, parquet: ParquetStore, activity_id: str) -> dict[str, Any]:
    group = resolve_activity(engine, activity_id)
    unified = _rows(engine, select(activity_unified).where(activity_unified.c.group_id == group))[0]
    members = []
    for key in unified["sources"]:
        source, source_id = key.split(":", 1)
        row = _rows(
            engine,
            select(activity).where(activity.c.source == source, activity.c.source_id == source_id),
        )
        if row:
            r = row[0]
            members.append(
                {"id": key, "name": r["name"], "device": r["device"], "extra": r["extra"]}
            )

    def fetch_laps(source, source_id):
        return _rows(
            engine,
            select(activity_lap)
            .where(activity_lap.c.source == source, activity_lap.c.activity_source_id == source_id)
            .order_by(activity_lap.c.lap_index),
        )

    def fetch_zones(source, source_id):
        return _rows(
            engine,
            select(
                activity_zone_time.c.source,
                activity_zone_time.c.activity_source_id,
                activity_zone_time.c.updated_at,
                activity_zone_time.c.kind,
                activity_zone_time.c.zone,
                activity_zone_time.c.seconds,
                activity_zone_time.c.low,
            )
            .where(
                activity_zone_time.c.source == source,
                activity_zone_time.c.activity_source_id == source_id,
            )
            .order_by(activity_zone_time.c.kind, activity_zone_time.c.zone),
        )

    laps, zones = select_laps_and_zones(unified["sources"], fetch_laps, fetch_zones)
    with engine.connect() as conn:
        weight_kg = conn.scalar(
            select(body_measurement.c.value)
            .where(
                body_measurement.c.metric == "body.weight",
                body_measurement.c.subject_id == unified["subject_id"],
                body_measurement.c.measured_at <= datetime.fromisoformat(unified["start_utc"]),
            )
            .order_by(body_measurement.c.measured_at.desc())
            .limit(1)
        )
    unified["sport_units"] = presentation(unified.get("sport"), unified, weight_kg=weight_kg)
    for lap in laps:
        lap["sport_units"] = presentation(unified.get("sport"), lap)
        for k in ("raw_item_id", "updated_at", "source", "activity_source_id"):
            lap.pop(k, None)
    stream_key = unified.get("stream_key")
    streams = None
    if stream_key and parquet.stream_path(stream_key).exists():
        table = parquet.read_stream(stream_key)
        streams = {
            "key": stream_key,
            "rows": table.num_rows,
            "channels": [c for c in table.column_names if c != "t"],
        }
    unified.pop("updated_at", None)
    from vibesynergy.query.session_purpose import _overrides

    with engine.connect() as conn:
        manual = _overrides(conn)
    # A purpose chosen on the activity: coaching or not, else automatic (None).
    chosen = next((manual[key] for key in unified["sources"] if key in manual), None)
    unified["coaching_override"] = None if chosen is None else chosen == "coached_by_me"
    result = {
        "activity": unified,
        "sources": members,
        "laps": laps,
        "zones": zones,
        "streams": streams,
        "notes": journal.notes_for_activity(engine, group),
    }
    result["evidence"] = evidence.activity_version(result, parquet)
    return result


# On-foot sports: devices record cadence per leg (strides/min); report steps/min instead.
_ON_FOOT = ("run", "walk", "hik")


def _on_foot(sport: str | None) -> bool:
    return bool(sport) and any(word in sport.lower() for word in _ON_FOOT)


def activity_streams(
    engine: Engine,
    parquet: ParquetStore,
    activity_id: str,
    *,
    channels: list[str] | None = None,
    max_points: int = 500,
) -> dict[str, Any]:
    group = resolve_activity(engine, activity_id)
    with engine.connect() as conn:
        row = conn.execute(
            select(activity_unified.c.stream_key, activity_unified.c.sport).where(
                activity_unified.c.group_id == group
            )
        ).first()
    key, sport = row if row else (None, None)
    if not key or not parquet.stream_path(key).exists():
        raise ValueError(f"{activity_id} has no stream data")
    cadence_factor = 2 if _on_foot(sport) else 1
    table = parquet.read_stream(key)
    available = [c for c in table.column_names if c != "t"]
    wanted = [c for c in (channels or available) if c in available]
    step = max(1, -(-table.num_rows // max_points))
    t = table.column("t")
    start = t[0].as_py()
    elapsed = [round((v.as_py() - start).total_seconds(), 1) for v in t[::step]]
    columns: dict[str, Any] = {"elapsed_s": elapsed}
    for name in wanted:
        col = table.column(name)
        if step > 1:  # mean over each bucket, not just every nth sample
            values = [pc.mean(col.slice(i, step)).as_py() for i in range(0, table.num_rows, step)]
        else:
            values = col.to_pylist()
        if name == "cadence" and cadence_factor != 1:
            values = [None if v is None else v * cadence_factor for v in values]
        columns[name] = [
            None if v is None else round(v, 5 if name in ("lat", "lon") else 2) for v in values
        ]
    return {
        "stream_key": key,
        "sport": sport,
        "speed_unit": pace_unit(sport),
        "start_utc": start.isoformat(),
        "samples": table.num_rows,
        "resampled_every": step,
        "available_channels": available,
        "columns": columns,
    }


# Profile, status, provenance ----------------------------------------------------------------


def profile(engine: Engine) -> dict[str, Any]:
    zones = _rows(
        engine,
        select(
            zone_set.c.source,
            zone_set.c.kind,
            zone_set.c.sport,
            zone_set.c.valid_from,
            zone_set.c.boundaries,
            zone_set.c.threshold,
            zone_set.c.max_value,
        ).order_by(zone_set.c.kind, zone_set.c.sport, desc(zone_set.c.valid_from)),
    )
    current: dict[tuple, dict] = {}
    for z in zones:
        current.setdefault((z["kind"], z["sport"]), z)
    return {"current_zones": list(current.values()), "zone_history": zones}


def _slice_sync_status(full: dict[str, Any], datasets: set[tuple[str, str]]) -> dict[str, Any]:
    rows = [row for row in full["datasets"] if (row["source"], row["dataset"]) in datasets]
    return {
        "datasets": rows,
        "problems": [row for row in rows if row["problem_stages"]],
        "semantics": full["semantics"],
    }


def sync_status(
    engine: Engine,
    source: str | None = None,
    *,
    datasets: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Fetch and processing are separate; optionally restrict status to selected datasets."""
    # One archive GROUP BY per connection: later calls reuse it, including a dataset slice.
    bound = getattr(engine, "conn", None)
    cache = bound.info.setdefault("vibesynergy_sync_status", {}) if bound is not None else None
    if cache is not None and source is None and datasets is None and "all" in cache:
        return cache["all"]
    if cache is not None and source is None and datasets is not None and "all" in cache:
        return _slice_sync_status(cache["all"], datasets)
    # A retired source's leftover state (its last failure) is not a problem to report.
    query = (
        select(dataset_state)
        .where(dataset_state.c.source.not_in(RETIRED_SOURCES))
        .order_by(dataset_state.c.source, dataset_state.c.dataset)
    )
    archive_query = (
        select(
            raw_item.c.source,
            raw_item.c.dataset,
            func.count().label("current_items"),
            func.count(raw_item.c.normalise_error).label("errors"),
            func.sum(case((raw_item.c.normalised_at.is_(None), 1), else_=0)).label("pending"),
            func.max(raw_item.c.normalised_at).label("last_attempt_at"),
            func.max(raw_item.c.last_seen_at).label("last_received_at"),
        )
        .where(raw_item.c.is_current.is_(True), raw_item.c.source.not_in(RETIRED_SOURCES))
        .group_by(raw_item.c.source, raw_item.c.dataset)
    )
    if source:
        query = query.where(dataset_state.c.source == source)
        archive_query = archive_query.where(raw_item.c.source == source)
    if datasets is not None:
        query = query.where(tuple_(dataset_state.c.source, dataset_state.c.dataset).in_(datasets))
        archive_query = archive_query.where(
            tuple_(raw_item.c.source, raw_item.c.dataset).in_(datasets)
        )
    dataset_rows = {(d["source"], d["dataset"]): d for d in _rows(engine, query)}
    archived = {(d["source"], d["dataset"]): d for d in _rows(engine, archive_query)}
    for key in dataset_rows.keys() | archived.keys():
        d = dataset_rows.setdefault(
            key,
            {
                "source": key[0],
                "dataset": key[1],
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": None,
                "synced_from": None,
                "synced_until": None,
            },
        )
        d.pop("cursor", None)
        a = archived.get(key, {})
        errors, pending = a.get("errors", 0), a.get("pending", 0)
        d["fetch_status"] = (
            "failed" if d["last_error"] else "succeeded" if d["last_success_at"] else "not_recorded"
        )
        d["processing"] = {
            "state": "failed"
            if errors
            else "pending"
            if pending
            else "processed"
            if a
            else "empty",
            "current_items": a.get("current_items", 0),
            "errors": errors,
            "pending": pending,
            "last_attempt_at": a.get("last_attempt_at"),
            "last_received_at": a.get("last_received_at"),
        }
        d["problem_stages"] = (["fetch"] if d["last_error"] else []) + (
            ["mapping"] if errors else []
        )
        messages = [d["last_error"]] if d["last_error"] else []
        if errors:
            messages.append(f"Mapping failed for {errors} current archive items")
        d["problem"] = "; ".join(messages) or None
    ordered = [dataset_rows[key] for key in sorted(dataset_rows)]
    result = {
        "datasets": ordered,
        "problems": [d for d in ordered if d["problem_stages"]],
        "semantics": {
            "last_success_at": "collection_or_import_only",
            "synced_range": "attempted_successfully_not_gap_free_coverage",
            "processing": "current_archive_versions_not_proof_of_mapped_metric_coverage",
        },
    }
    if cache is not None and source is None and datasets is None:
        cache["all"] = result
    return result


def freshness(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(dataset_state.c.source, func.max(dataset_state.c.last_success_at))
            .where(dataset_state.c.source.not_in(RETIRED_SOURCES))
            .group_by(dataset_state.c.source)
        ).all()
    return {s: t.isoformat() for s, t in rows if t}


# A metric's expected observation cadence is separate from its source fetch schedule.
# None means no age claim can be made from this catalogue entry.
METRIC_MAX_AGE_DAYS = {
    "sleep.duration": 1,
    "sleep.score": 1,
    "hrv.night_avg": 1,
    "hr.resting": 1,
    "recovery.readiness": 1,
    "daily.steps": 1,
    "body.weight": 14,
}


def selected_metric_status(engine: Engine, metric: str, window: Window) -> dict | None:
    """Status for the latest best-source observation, using the shared metric contract.

    Selection only identifies a source/dataset; all freshness and failure decisions remain
    in metric_status. A missing observation has no defensible source to assess.
    """
    with engine.connect() as conn:
        selected = conn.execute(
            select(daily_metric.c.source, raw_item.c.dataset)
            .join(
                daily_unified,
                and_(
                    daily_unified.c.subject_id == daily_metric.c.subject_id,
                    daily_unified.c.date == daily_metric.c.date,
                    daily_unified.c.metric == daily_metric.c.metric,
                    daily_unified.c.source == daily_metric.c.source,
                ),
            )
            .join(raw_item, raw_item.c.id == daily_metric.c.raw_item_id)
            .where(
                daily_metric.c.subject_id == 1,
                daily_metric.c.metric == metric,
                daily_metric.c.date <= window.end,
            )
            .order_by(daily_metric.c.date.desc())
            .limit(1)
        ).first()
    return metric_status(engine, metric, selected[0], selected[1], window) if selected else None


def metric_status(
    engine: Engine,
    metric: str,
    source: str,
    dataset: str,
    window: Window,
    *,
    previous_version: str | None = None,
) -> dict[str, Any]:
    """Observed daily values plus one source/dataset's collection and mapping status.

    This is a source-specific observation contract, not an assertion that a period is
    completely sampled. A version comparison is only `unchanged` after a successful,
    current read. Historical dates are judged against the requested interval end.
    """
    if not source or not dataset:
        raise ValueError("source and dataset are required for metric status")
    from vibesynergy.query import catalog

    if metric in catalog.COMPUTED:
        raise ValueError("metric status requires a canonical daily metric")
    definition = catalog.catalogue(engine, coverage=False).get(metric)
    if definition is None:
        catalog.check_metrics(engine, [metric])  # includes close-match suggestions
    metric_unit = definition["unit"]
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                daily_metric.c.date,
                daily_metric.c.value,
                daily_metric.c.unit,
                raw_item.c.fetched_at,
                raw_item.c.normalised_at,
            )
            .join(raw_item, daily_metric.c.raw_item_id == raw_item.c.id)
            .where(
                daily_metric.c.metric == metric,
                daily_metric.c.subject_id == 1,
                daily_metric.c.source == source,
                daily_metric.c.date.between(window.start, window.end),
                raw_item.c.dataset == dataset,
            )
            .order_by(daily_metric.c.date)
        ).all()
    observations = [
        {
            "date": str(day),
            "value": value,
            "unit": unit,
            "source": source,
            "dataset": dataset,
            "fetched_at": fetched.isoformat(),
            "mapped_at": mapped.isoformat() if mapped else None,
        }
        for day, value, unit, fetched, mapped in rows
    ]
    status = next(
        (item for item in sync_status(engine, source)["datasets"] if item["dataset"] == dataset),
        None,
    )
    problem = None
    if status and status["processing"]["state"] == "failed":
        problem = {
            "source": source,
            "dataset": dataset,
            "stage": "mapping",
            "error": status["problem"],
        }
    elif status and status["fetch_status"] == "failed":
        problem = {
            "source": source,
            "dataset": dataset,
            "stage": "fetch",
            "error": status["last_error"],
        }
    if problem:
        freshness_state = "unavailable"
    elif status and status["processing"]["state"] == "pending":
        freshness_state = "pending"
    elif not status or (
        status["fetch_status"] == "not_recorded"
        and status["processing"]["state"] == "empty"
        and not observations
    ):
        freshness_state = "unavailable"
        problem = {"source": source, "dataset": dataset, "stage": "fetch", "error": "not_recorded"}
    elif not observations:
        freshness_state = "empty"
    else:
        last_day = date.fromisoformat(observations[-1]["date"])
        age = (window.end - last_day).days
        limit = METRIC_MAX_AGE_DAYS.get(metric)
        freshness_state = "stale" if limit is not None and age > limit else "current"
    coverage = {
        "from": str(window.start),
        "through": str(window.end),
        "observed_days": len({item["date"] for item in observations}),
        "days_in_window": len(window),
        "latest_observation": observations[-1]["date"] if observations else None,
        "max_age_days": METRIC_MAX_AGE_DAYS.get(metric),
    }
    version = evidence.digest(
        {
            "contract": 1,
            "metric": metric,
            "source": source,
            "dataset": dataset,
            "window": [str(window.start), str(window.end)],
            "observations": [(o["date"], o["value"], o["unit"]) for o in observations],
        }
    )
    return {
        "contract_version": 1,
        "metric": metric,
        "unit": observations[-1]["unit"] if observations else metric_unit,
        "source": source,
        "dataset": dataset,
        "state": "unchanged"
        if freshness_state == "current" and previous_version == version
        else freshness_state,
        "freshness": freshness_state,
        "version": version,
        "coverage": coverage,
        "observations": observations,
        "collection": status,
        "problem": problem,
    }


def default_source_order(
    engine: Engine, prefixes: list[str], eligible: set[str]
) -> dict[str, list[str]]:
    """Per metric prefix, the sources to prefer when nothing is overridden: the connected
    (``eligible``) sources that have data for it, in the built-in hot-to-cold order. A source
    the built-in order does not know yet comes last, like in the daily rebuild."""
    with engine.connect() as conn:
        pairs = conn.execute(
            select(daily_metric.c.source, daily_metric.c.metric)
            .where(daily_metric.c.subject_id == 1)
            .group_by(daily_metric.c.source, daily_metric.c.metric)
        ).all()
    have: dict[str, set[str]] = {prefix: set() for prefix in prefixes}
    for source, metric in pairs:
        if source in eligible:
            # The most specific listed prefix owns the metric, as in the rebuild.
            owner = max((p for p in prefixes if metric.startswith(p)), key=len, default=None)
            if owner is not None:
                have[owner].add(source)
    out = {}
    for prefix, sources in have.items():
        ranked = [s for s in source_order(prefix) if s in sources]
        out[prefix] = ranked + sorted(sources - set(ranked))
    return out


def explain(
    engine: Engine,
    metric: str,
    when: date,
    best_source: dict | None = None,
    exact: dict | None = None,
) -> dict[str, Any]:
    """Which source's value is shown, the alternatives, and the raw record behind each."""
    chosen = _rows(
        engine,
        select(daily_unified).where(daily_unified.c.metric == metric, daily_unified.c.date == when),
    )
    alternatives = _rows(
        engine,
        select(
            daily_metric.c.source,
            daily_metric.c.value,
            daily_metric.c.unit,
            daily_metric.c.raw_item_id,
            raw_item.c.dataset,
            raw_item.c.item_key,
            raw_item.c.fetched_at,
        )
        .join(raw_item, raw_item.c.id == daily_metric.c.raw_item_id)
        .where(daily_metric.c.metric == metric, daily_metric.c.date == when),
    )
    rules = {**DEFAULT_BEST_SOURCE, **(best_source or {})}
    rule = next((p for p in sorted(rules, key=len, reverse=True) if metric.startswith(p)), "")
    if exact and exact.get(metric):  # the order saved for this metric on the Settings page
        rule, rules = metric, {metric: list(exact[metric])}
    return {
        "metric": metric,
        "date": str(when),
        "shown": chosen[0] if chosen else None,
        "all_sources": alternatives,
        "best_source_rule": {
            "prefix": rule or "(default)",
            "order": rules[rule],
        },
        "definition": KNOWN[metric].to_dict() if metric in KNOWN else None,
    }


def coverage(engine: Engine, metrics: list[str], window: Window) -> dict[str, Any]:
    """Days with data per month and metric."""
    values = qs.daily_values(engine, metrics, window)
    months = [str(m.start)[:7] for m in window.months()]
    matrix = {m: {month: 0 for month in months} for m in metrics}
    for metric, by_day in values.items():
        for d in by_day:
            matrix[metric][str(d)[:7]] += 1
    days_in_window = {
        str(m.start)[:7]: len(Window(max(m.start, window.start), min(m.end, window.end)))
        for m in window.months()
    }
    return {"months": months, "days_with_data": matrix, "days_in_window": days_in_window}


def overview(engine: Engine, today: date) -> dict[str, Any]:
    from vibesynergy.query import plan as plan_q

    week = Window(today - timedelta(days=6), today)
    key = [
        "sleep.score",
        "hrv.night_avg",
        "hr.resting",
        "recovery.readiness",
        "body.weight",
        "daily.steps",
        "activity.count",
        "activity.duration",
    ]
    with engine.connect() as conn:
        counts = {
            "activities": conn.execute(select(func.count()).select_from(activity_unified)).scalar(),
            "days_with_data": conn.execute(
                select(func.count(daily_unified.c.date.distinct()))
            ).scalar(),
            "first_date": str(conn.execute(select(func.min(daily_unified.c.date))).scalar()),
        }
    status = sync_status(engine)
    return {
        **plan_q.race_phase(engine, today),
        "store": counts,
        "last_7_days": qs.series(engine, key, week, granularity="day"),
        "sync_problems": [
            {
                "dataset": f"{p['source']}.{p['dataset']}",
                "error": p["problem"],
                "stages": p["problem_stages"],
            }
            for p in status["problems"]
        ],
        "sources": sorted({d["source"] for d in status["datasets"]}),
    }


# Blood pressure and intensity distribution --------------------------------------------------


def blood_pressure(engine: Engine, window: Window, timezone: str) -> dict[str, Any]:
    """Readings grouped per measurement, split morning/evening, with the ACC/AHA 2017
    category of each (informational only)."""
    tz = ZoneInfo(timezone)
    rows = _rows(
        engine,
        select(
            body_measurement.c.source,
            body_measurement.c.source_id,
            body_measurement.c.measured_at,
            body_measurement.c.metric,
            body_measurement.c.value,
        )
        .where(
            body_measurement.c.metric.in_(["bp.systolic", "bp.diastolic", "bp.pulse"]),
            body_measurement.c.measured_at.between(
                _utc_day(window.start, -1), _utc_day(window.end, 2)
            ),
        )
        .order_by(body_measurement.c.measured_at),
    )
    readings: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        local = datetime.fromisoformat(r["measured_at"]).astimezone(tz)
        if not window.start <= local.date() <= window.end:
            continue
        reading = readings.setdefault(
            (r["source"], r["source_id"]),
            {"local_time": local.isoformat(timespec="minutes"), "source": r["source"]},
        )
        reading[r["metric"].split(".", 1)[1]] = r["value"]
    out = [r for r in readings.values() if "systolic" in r and "diastolic" in r]
    for r in out:
        r["category"] = _bp_category(r["systolic"], r["diastolic"])
        r["part_of_day"] = "morning" if int(r["local_time"][11:13]) < 12 else "evening"

    def average(items: list[dict]) -> dict[str, Any] | None:
        if not items:
            return None
        return {
            "n": len(items),
            "systolic": round(sum(i["systolic"] for i in items) / len(items), 1),
            "diastolic": round(sum(i["diastolic"] for i in items) / len(items), 1),
        }

    return {
        "readings": out,
        "average": average(out),
        "morning": average([r for r in out if r["part_of_day"] == "morning"]),
        "evening": average([r for r in out if r["part_of_day"] == "evening"]),
        "categories": "ACC/AHA 2017: normal <120/<80, elevated 120-129/<80, stage 1 130-139 "
        "or 80-89, stage 2 >=140 or >=90",
    }


def _bp_category(systolic: float, diastolic: float) -> str:
    if systolic >= 140 or diastolic >= 90:
        return "stage 2"
    if systolic >= 130 or diastolic >= 80:
        return "stage 1"
    if systolic >= 120:
        return "elevated"
    return "normal"


def intensity_distribution(
    engine: Engine,
    window: Window,
    *,
    basis: str = "hr",
    include_coaching: bool = True,
    raw=None,
) -> dict:
    """Time per zone over the period, per week and in total. Heart rate, power and pace
    each keep their own zone count, from the first source copy that has zone times
    (Garmin, measured with the zones VibeSynergy keeps there). ``raw`` is not used. For five heart
    rate zones the zones are also folded into three (1-2 low, 3 moderate, 4-5 high) with
    the polarisation index of Treff et al. (2019): log10(low/moderate x high x 100) on
    fractions. Power and pace are not folded."""
    if basis not in ("hr", "power", "pace"):
        raise ValueError("basis must be hr, power or pace")
    groups = _rows(
        engine,
        select(
            activity_unified.c.group_id,
            activity_unified.c.local_date,
            activity_unified.c.start_utc,
            activity_unified.c.sources,
            activity_unified.c.kind,
        ).where(activity_unified.c.local_date.between(window.start, window.end)),
    )
    del raw
    weekly: dict[str, dict[int, float]] = {}
    total: dict[int, float] = {}
    coaching_total: dict[int, float] = {}
    counted = 0
    by_source = {"other": 0}

    def add(g: dict, zones) -> None:
        week = str(qs.bucket(date.fromisoformat(str(g["local_date"])[:10]), "week"))
        for zone, seconds in zones:
            weekly.setdefault(week, {}).setdefault(zone, 0.0)
            weekly[week][zone] += seconds
            total[zone] = total.get(zone, 0.0) + seconds
            if g["kind"] == "coaching":
                coaching_total[zone] = coaching_total.get(zone, 0.0) + seconds

    with engine.connect() as conn:
        for g in groups:
            if g["kind"] == "coaching" and not include_coaching:
                continue
            for key in g["sources"] or []:  # the first copy that has zone times
                source, source_id = key.split(":", 1)
                zones = conn.execute(
                    select(activity_zone_time.c.zone, activity_zone_time.c.seconds).where(
                        activity_zone_time.c.source == source,
                        activity_zone_time.c.activity_source_id == source_id,
                        activity_zone_time.c.kind == basis,
                    )
                ).all()
                if zones:
                    counted += 1
                    by_source["other"] += 1
                    add(g, zones)
                    break
    result: dict[str, Any] = {
        "basis": basis,
        "activities_with_zones": counted,
        "activities_by_zone_source": by_source,
        "total_seconds_by_zone": dict(sorted(total.items())),
        "coaching_seconds_by_zone": dict(sorted(coaching_total.items())),
        "weekly_seconds_by_zone": {w: dict(sorted(z.items())) for w, z in sorted(weekly.items())},
    }
    all_time = sum(total.values())
    # The fold into three only reads five zones (intervals.icu can be set up with more).
    if basis == "hr" and all_time and max(total) <= 5:
        low = (total.get(1, 0) + total.get(2, 0)) / all_time
        moderate = total.get(3, 0) / all_time
        high = (total.get(4, 0) + total.get(5, 0)) / all_time
        result["three_zone_share"] = {
            "low": round(low, 3),
            "moderate": round(moderate, 3),
            "high": round(high, 3),
        }
        if low and moderate and high:
            result["polarisation_index"] = round(math.log10(low / moderate * high * 100), 2)
    return result
