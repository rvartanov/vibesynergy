"""Garmin account data export (the "GDPR" zip from garmin.com/account): history backfill.

``vibesynergy import garmin-export <zip>`` stores every file of the export in the raw archive
under source ``garmin_export``: FIT files (from the nested UploadedFiles zips) by content
hash, all other files by their path in the zip. Its rows are attributed to ``garmin_export``,
not ``garmin``, so API data and export data never overwrite each other; phase 2 links the
two copies of each activity (same activityId).

The JSON layouts below are the ones the export is known to use. They are not documented by
Garmin and change over the years: anything a mapper does not recognise stays in the raw
archive, and after a mapper fix ``vibesynergy normalise garmin_export`` re-maps it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from vibesynergy.core.timeutil import Window
from vibesynergy.fit.decode import map_fit
from vibesynergy.model import Activity, DailyMetric, Record, SleepSession
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.sources.garmin.mappers import USER_SUMMARY, metrics_from, moving_seconds, num
from vibesynergy.store.raw import RawRecord

# The FIT files in DI-Connect-Uploaded-Files are named "<something>_<activityId>.fit" or
# "<activityId>.fit"; the id links the file to its activity.
_ACTIVITY_ID_IN_NAME = re.compile(r"(?:^|[_/])(\d{6,})(?:_[A-Z]+)?\.fit$", re.IGNORECASE)


class GarminExportSource(Source):
    id = "garmin_export"
    name = "Garmin account export (import only)"

    def datasets(self) -> Sequence[DatasetSpec]:
        return [
            DatasetSpec("files", DatasetKind.FILE, "Export files other than FIT, by path"),
            DatasetSpec("fit", DatasetKind.FILE, "FIT files from the export, by content hash"),
        ]

    def fetch(self, dataset: DatasetSpec, window: Window | None):
        return iter(())  # import only

    def mappers(self) -> Mapping[str, Mapper]:
        timezone = self.ctx.settings.timezone

        def fit(raw: RawRecord) -> Iterable[Record]:
            path = str(raw.request.get("path", ""))
            match = _ACTIVITY_ID_IN_NAME.search(path)
            return map_fit(
                raw.payload, timezone=timezone, source_activity_id=match.group(1) if match else None
            )

        return {"files": map_export_file, "fit": fit}


def item_for(path: str, data: bytes, *, is_fit: bool, sha256: str) -> tuple[str, FetchedItem]:
    """The dataset and raw item for one file of the export."""
    if is_fit:
        return "fit", FetchedItem(sha256, data, request={"path": path}, immutable=True)
    content_type = "application/json" if path.lower().endswith(".json") else None
    return "files", FetchedItem(
        path, data, request={"path": path}, content_type=content_type or "application/octet-stream"
    )


# Mapping export files by name ---------------------------------------------------------------


def map_export_file(raw: RawRecord) -> Iterable[Record]:
    name = raw.item_key.rsplit("/", 1)[-1]
    # macOS resource/attribute sidecars, not the adjacent JSON data fork (RFC 1740 App. B).
    # Require the name AND magic so malformed real JSON is still reported as an error.
    if name.startswith("._") and raw.payload.startswith(b"\x00\x05\x16\x07"):
        return []
    if not name.lower().endswith(".json"):
        return []
    for pattern, mapper in _FILE_MAPPERS:
        if pattern.search(name):
            return mapper(raw.json())
    return []


def _map_summarized_activities(data: Any) -> Iterator[Record]:
    """``*_summarizedActivities.json``: [{"summarizedActivitiesExport": [activity, …]}].

    Units differ from the API: times are epoch milliseconds, durations milliseconds,
    distance and elevation centimetres.
    """
    for block in _as_list(data):
        for act in block.get("summarizedActivitiesExport") or []:
            activity_id, start_ms = act.get("activityId"), num(act.get("startTimeGmt"))
            if activity_id is None or start_ms is None:
                continue
            start = datetime.fromtimestamp(start_ms / 1000, UTC)
            local_ms = num(act.get("startTimeLocal"))
            offset = round((local_ms - start_ms) / 1000) if local_ms is not None else None
            local_date = (start + timedelta(seconds=offset or 0)).date()
            mapped = {
                "activityId", "name", "activityType", "startTimeGmt", "startTimeLocal",
                "duration", "elapsedDuration", "movingDuration", "distance", "elevationGain",
                "avgHr", "maxHr", "avgPower", "normPower", "aerobicTrainingEffect",
                "anaerobicTrainingEffect", "activityTrainingLoad", "deviceId",
            }  # fmt: skip
            yield Activity(
                source_id=str(activity_id),
                start_utc=start,
                tz_offset_s=offset,
                local_date=local_date,
                sport=str(act.get("activityType") or act.get("sportType") or "unknown"),
                name=act.get("name"),
                elapsed_s=_scaled(act.get("elapsedDuration") or act.get("duration"), 1000),
                moving_s=moving_seconds(
                    act.get("activityType") or act.get("sportType"),
                    _scaled(act.get("movingDuration") or act.get("duration"), 1000),
                    _scaled(act.get("elapsedDuration") or act.get("duration"), 1000),
                ),
                distance_m=_scaled(act.get("distance"), 100),
                ascent_m=_scaled(act.get("elevationGain"), 100),
                avg_hr=num(act.get("avgHr")),
                max_hr=num(act.get("maxHr")),
                avg_power=num(act.get("avgPower")),
                norm_power=num(act.get("normPower")),
                training_load=num(act.get("activityTrainingLoad")),
                aerobic_te=num(act.get("aerobicTrainingEffect")),
                anaerobic_te=num(act.get("anaerobicTrainingEffect")),
                device=str(act["deviceId"]) if act.get("deviceId") else None,
                extra={k: v for k, v in act.items() if k not in mapped and v is not None},
            )


def _map_uds(data: Any) -> Iterator[Record]:
    """``UDSFile_*.json``: one daily summary per day, fields as in the API user summary."""
    for day_data in _as_list(data):
        day = _date(day_data.get("calendarDate"))
        if day:
            yield from metrics_from(day_data, day, USER_SUMMARY)


def _map_sleep(data: Any) -> Iterator[Record]:
    """``*_sleepData.json``: one entry per night; no total, so it is the sum of stages."""
    for night in _as_list(data):
        day = _date(night.get("calendarDate"))
        stages = {
            key: num(night.get(f"{key}SleepSeconds")) for key in ("deep", "light", "rem", "awake")
        }
        if not day or not any(stages.values()):
            continue
        asleep = sum(v or 0 for k, v in stages.items() if k != "awake")
        score = num((night.get("sleepScores") or {}).get("overallScore"))
        for key, value in stages.items():
            if value is not None:
                yield DailyMetric(day, f"sleep.{key}", value, "s")
        yield DailyMetric(day, "sleep.duration", asleep, "s")
        if score is not None:
            yield DailyMetric(day, "sleep.score", score, "score")
        yield SleepSession(
            source_id=day.isoformat(),
            local_date=day,
            start_utc=_gmt_text(night.get("sleepStartTimestampGMT")),
            end_utc=_gmt_text(night.get("sleepEndTimestampGMT")),
            duration_s=asleep,
            deep_s=stages["deep"],
            light_s=stages["light"],
            rem_s=stages["rem"],
            awake_s=stages["awake"],
            score=score,
            extra={k: v for k, v in night.items() if not k.endswith("SleepSeconds")},
        )


def _map_max_met(data: Any) -> Iterator[Record]:
    """``MetricsMaxMetData_*.json``: VO2max per day and sport."""
    for entry in _as_list(data):
        day = _date(entry.get("calendarDate"))
        value = num(entry.get("vo2MaxValue"))
        if not day or value is None:
            continue
        sport = str(entry.get("sport") or "RUNNING").upper()
        metric = "fitness.vo2max.ride" if sport == "CYCLING" else "fitness.vo2max.run"
        yield DailyMetric(day, metric, value, "ml_kg_min")


_FILE_MAPPERS = [
    (re.compile(r"summarizedActivities\.json$"), _map_summarized_activities),
    (re.compile(r"^UDSFile_.*\.json$"), _map_uds),
    (re.compile(r"sleepData\.json$"), _map_sleep),
    (re.compile(r"^MetricsMaxMetData.*\.json$"), _map_max_met),
]


def _as_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def _date(value: Any) -> date | None:
    if isinstance(value, dict):  # some files wrap dates: {"date": "…"}
        value = value.get("date")
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _scaled(value: Any, divisor: float) -> float | None:
    number = num(value)
    return number / divisor if number is not None else None


def _gmt_text(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except ValueError:
        return None
