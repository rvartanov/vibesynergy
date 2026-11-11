"""Content versions for recorded activity evidence, independent of polling and prose.

This is an input fingerprint, not a coach assessment or authority for external writes.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vibesynergy.store.parquet import ParquetStore

VERSION = 1
# Names/descriptions, fetch times, source ranking and derived group IDs are not measurements.
FIELDS = (
    "subject_id",
    "start_utc",
    "local_date",
    "tz_offset_s",
    "sport",
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
ZONE_FIELDS = ("kind", "zone", "seconds", "low")


def json_default(value):
    """``json.dumps`` default for evidence: dates and datetimes as ISO text."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def digest(value) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=json_default, allow_nan=False
    )
    return hashlib.sha256(content.encode()).hexdigest()


def _stamp(path: Path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@lru_cache(maxsize=1024)
def _stream_digest(path_string: str, stamp: tuple) -> str:
    path = Path(path_string)
    table = pq.read_table(path)
    # Normalize column/chunk order and metadata before hashing the same sample content.
    table = table.select(sorted(table.column_names)).combine_chunks().replace_schema_metadata(None)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    result = hashlib.sha256(sink.getvalue()).hexdigest()
    if _stamp(path) != stamp:
        raise ValueError("activity stream changed while reading; retry the evidence snapshot")
    return result


def warm_stream_digests(paths: list[Path]) -> None:
    """Hash stream files ahead of a bulk snapshot. Same digest as ``activity_version``."""
    pending = [path for path in paths if path.is_file()]
    if len(pending) < 2:
        return

    def one(path: Path) -> None:
        stamp = _stamp(path)
        _stream_digest(str(path), stamp)

    workers = min(4, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, pending))


def activity_version(detail: dict, parquet: ParquetStore) -> dict:
    """For the existing activity-detail response shared by portal and MCP.

    Persist source references to resolve a later group; group IDs can change after dedup.
    A downstream action must recapture and compare versions before acting.
    """
    activity = detail["activity"]
    key = activity.get("stream_key")
    path = parquet.stream_path(key) if key else None
    if path is not None and path.is_file():
        stamp = _stamp(path)
        stream = {"state": "available", "version": _stream_digest(str(path), stamp)}
        if _stamp(path) != stamp:
            raise ValueError("activity stream changed while reading; retry the evidence snapshot")
    else:
        stream = {"state": "absent", "version": None}
    zones = [{k: z.get(k) for k in ZONE_FIELDS} for z in detail["zones"]]
    zones.sort(key=lambda z: (z["kind"], z["zone"]))
    laps = sorted(
        ({k: v for k, v in lap.items() if k != "sport_units"} for lap in detail["laps"]),
        key=lambda lap: lap["lap_index"],
    )
    payload = {
        "contract_version": VERSION,
        "recorded": {k: activity.get(k) for k in FIELDS},
        "laps": laps,
        "zones": zones,
        "stream": stream,
    }
    return {
        "contract_version": VERSION,
        "facts_version": digest(payload),
        "source_refs": sorted(activity["sources"]),
        "current_group_id": activity["group_id"],
        "stream": stream,
        "scope": "recorded_activity_inputs",
        "excludes": ["titles", "descriptions", "diary", "coach_context", "plan", "retrieval_times"],
    }
