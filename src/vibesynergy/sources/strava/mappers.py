"""Strava raw items -> canonical records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from vibesynergy.model import Activity, ActivityLap, ActivityZoneTime, Record, Stream
from vibesynergy.store.raw import RawRecord

_EXTRA = (
    "type", "external_id", "upload_id", "gear_id", "trainer", "commute", "manual",
    "private", "description", "kilojoules", "suffer_score", "workout_type", "timezone",
    "device_watts", "has_heartrate", "average_cadence", "max_watts", "max_speed",
)  # fmt: skip

# Stream type -> channel name (same channels as FIT streams).
_CHANNELS = {
    "distance": "dist",
    "altitude": "alt",
    "velocity_smooth": "speed",
    "heartrate": "hr",
    "cadence": "cadence",
    "watts": "power",
    "temp": "temp",
    "moving": "moving",
    "grade_smooth": "grade",
}


def stream_key(activity_id: str) -> str:
    return f"strava-{activity_id}"


def map_activity(raw: RawRecord) -> Iterator[Record]:
    data = raw.json() or {}
    if data.get("deleted"):  # tombstone from a webhook delete event: no rows
        return
    a = {**(data.get("summary") or {}), **(data.get("detail") or {})}
    start = _utc(a.get("start_date"))
    if start is None:
        raise ValueError(f"activity {raw.item_key} has no start_date")
    local_date = date.fromisoformat(str(a.get("start_date_local") or a["start_date"])[:10])
    activity_id = str(a.get("id") or raw.item_key)
    extra = {k: a[k] for k in _EXTRA if a.get(k) is not None}
    if data.get("errors"):
        extra["detail_errors"] = data["errors"]
    yield Activity(
        source_id=activity_id,
        start_utc=start,
        tz_offset_s=int(a["utc_offset"]) if a.get("utc_offset") is not None else None,
        local_date=local_date,
        sport=str(a.get("sport_type") or a.get("type") or "unknown"),
        name=a.get("name"),
        elapsed_s=_num(a.get("elapsed_time")),
        moving_s=_num(a.get("moving_time")),
        distance_m=_num(a.get("distance")),
        ascent_m=_num(a.get("total_elevation_gain")),
        avg_hr=_num(a.get("average_heartrate")),
        max_hr=_num(a.get("max_heartrate")),
        avg_power=_num(a.get("average_watts")),
        norm_power=_num(a.get("weighted_average_watts")),
        avg_speed=_num(a.get("average_speed")),
        calories=_num(a.get("calories")),
        device=a.get("device_name"),
        fit_id=stream_key(activity_id) if not a.get("manual") else None,
        extra=extra,
    )
    for lap in data.get("laps") or []:
        index = lap.get("lap_index")
        if index is None:
            continue
        yield ActivityLap(
            activity_source_id=activity_id,
            lap_index=int(index),
            start_utc=_utc(lap.get("start_date")),
            elapsed_s=_num(lap.get("elapsed_time")),
            moving_s=_num(lap.get("moving_time")),
            distance_m=_num(lap.get("distance")),
            avg_hr=_num(lap.get("average_heartrate")),
            max_hr=_num(lap.get("max_heartrate")),
            avg_power=_num(lap.get("average_watts")),
            avg_speed=_num(lap.get("average_speed")),
        )
    zones = data.get("zones")
    for block in zones if isinstance(zones, list) else []:
        kind = {"heartrate": "hr", "power": "power"}.get(block.get("type"))
        if not kind:
            continue
        for number, bucket in enumerate(block.get("distribution_buckets") or [], start=1):
            seconds = _num(bucket.get("time"))
            if seconds is not None:
                yield ActivityZoneTime(activity_id, kind, number, seconds, _num(bucket.get("min")))


def map_streams(raw: RawRecord) -> Iterator[Record]:
    data = raw.json() or {}
    streams = data.get("streams") or {}
    start = _utc(data.get("start_date"))
    times = (streams.get("time") or {}).get("data") or []
    if start is None or not times:
        return
    columns: dict[str, list[Any]] = {"t": [start + timedelta(seconds=s) for s in times]}
    latlng = (streams.get("latlng") or {}).get("data")
    if latlng and len(latlng) == len(times):
        columns["lat"] = [p[0] if p else None for p in latlng]
        columns["lon"] = [p[1] if p else None for p in latlng]
    for name, channel in _CHANNELS.items():
        values = (streams.get(name) or {}).get("data")
        if values and len(values) == len(times):
            columns[channel] = [_num(v) for v in values]
    yield Stream(stream_key(str(data.get("activity_id") or raw.item_key)), columns)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
