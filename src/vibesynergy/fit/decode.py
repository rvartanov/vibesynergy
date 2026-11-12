"""Decode FIT files with fitdecode and turn them into canonical records.

Activity files become a ``Stream`` (per-second channels, developer fields included), a
``FitFile`` row, the heart-rate/power/pace zones the device used at the time, the device's own
recorded time per pace zone when present, and, when asked, an ``Activity`` with laps.
Monitoring files (the daily wellness FIT) become ``WellnessSamples``.

Pace-zone boundaries and recorded zone time come only from the device's own FIT messages
(``speed_zone``, ``time_in_zone``); Garmin Connect's and Strava's REST APIs do not expose a
pace-zone contract, and pace is never inferred from heart rate or power.

A FIT file is identified across copies by ``fit_id``: manufacturer, serial number and
creation time from its ``file_id`` message, so the same ride downloaded from the API, found
in the account export and in an old folder maps to the same stream file.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import fitdecode
from fitdecode.types import DevFieldDefinition

from vibesynergy.model import (
    Activity,
    ActivityLap,
    ActivityZoneTime,
    FitFile,
    Record,
    Stream,
    WellnessSamples,
    ZoneSet,
)

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z in Unix seconds
_SEMICIRCLE = 180 / 2**31

# Record fields renamed to the stream's channel names; the rest keep their FIT name.
_CHANNELS = {
    "position_lat": "lat",
    "position_long": "lon",
    "enhanced_altitude": "alt",
    "altitude": "alt",
    "distance": "dist",
    "enhanced_speed": "speed",
    "speed": "speed",
    "heart_rate": "hr",
    "power": "power",
    "temperature": "temp",
}
# Where both exist, the enhanced field wins.
_PREFERRED = {"altitude": "enhanced_altitude", "speed": "enhanced_speed"}
_SKIP_FIELDS = {"timestamp", "compressed_speed_distance", "fractional_cadence"}


def is_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == b".FIT"


_MAX_ZIP_DEPTH = 3
_MAX_FIT_MEMBER_BYTES = 512 * 1024**2


def fit_files_in(data: bytes, name: str = "", *, _depth: int = 0) -> Iterator[tuple[str, bytes]]:
    """Yield ``(name, bytes)`` for a FIT file or every FIT file inside a zip (recursively).

    Bounded: a zip nested deeper than a download ever is, or a member far larger than a FIT
    file, is skipped rather than unpacked into memory."""
    if is_fit(data):
        yield name, data
        return
    if _depth >= _MAX_ZIP_DEPTH or not zipfile.is_zipfile(io.BytesIO(data)):
        return
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if not info.is_dir() and info.file_size <= _MAX_FIT_MEMBER_BYTES:
                yield from fit_files_in(zf.read(info), info.filename, _depth=_depth + 1)


@dataclass
class DecodedFit:
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sha256: str = ""

    def first(self, name: str) -> dict[str, Any]:
        found = self.messages.get(name)
        return found[0] if found else {}

    @property
    def file_type(self) -> str:
        value = self.first("file_id").get("type")
        return str(value) if value is not None else "unknown"

    @property
    def fit_id(self) -> str:
        fid = self.first("file_id")
        created = fid.get("time_created")
        if isinstance(created, datetime):
            maker = fid.get("manufacturer") or "unknown"
            serial = fid.get("serial_number") or 0
            return f"{maker}-{serial}-{int(created.timestamp())}"
        return f"sha-{self.sha256[:24]}"


def decode(data: bytes) -> DecodedFit:
    """Read every data message; developer fields are prefixed ``dev_``."""
    decoded = DecodedFit(sha256=hashlib.sha256(data).hexdigest())
    with fitdecode.FitReader(
        io.BytesIO(data),
        check_crc=fitdecode.CrcCheck.WARN,
        error_handling=fitdecode.ErrorHandling.WARN,
    ) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            values: dict[str, Any] = {}
            for fd in frame.fields:
                if fd.value is None:
                    continue
                if isinstance(fd.field_def, DevFieldDefinition):
                    values[f"dev_{fd.name}"] = fd.value
                else:
                    values.setdefault(fd.name, fd.value)
            decoded.messages.setdefault(frame.name, []).append(values)
    return decoded


def map_fit(
    data: bytes,
    *,
    timezone: str,
    source_activity_id: str | None = None,
    with_activity: bool = False,
) -> list[Record]:
    """Canonical records for one FIT file.

    ``with_activity`` also yields an ``Activity`` (keyed by fit_id) and its laps; used for
    files that have no API summary, such as old head-unit files.
    """
    fit = decode(data)
    session = fit.first("session")
    start = _as_utc(session.get("start_time")) or _first_timestamp(fit, "record")
    records: list[Record] = [
        FitFile(
            fit_id=fit.fit_id,
            file_type=fit.file_type,
            time_created=_as_utc(fit.first("file_id").get("time_created")),
            manufacturer=_str(fit.first("file_id").get("manufacturer")),
            product=_str(
                fit.first("file_id").get("garmin_product") or fit.first("file_id").get("product")
            ),
            serial_number=_int(fit.first("file_id").get("serial_number")),
            start_utc=start,
            sport=_str(session.get("sport")),
            source_activity_id=source_activity_id,
            messages=dict(Counter({k: len(v) for k, v in fit.messages.items()})),
        )
    ]
    if fit.file_type == "activity":
        offset = _tz_offset(fit, start, timezone)
        local_date = (start + timedelta(seconds=offset)).date() if start else None
        if stream := _stream(fit):
            records.append(stream)
        if local_date:
            records.extend(_zones(fit, local_date))
        records.extend(_pace_zone_times(fit, source_activity_id or fit.fit_id))
        if with_activity and start and local_date:
            records.append(_activity(fit, session, start, offset, local_date))
            records.extend(_laps(fit))
    elif fit.file_type.startswith("monitoring") or fit.messages.get("monitoring"):
        if samples := _wellness(fit):
            records.append(samples)
    return records


def _stream(fit: DecodedFit) -> Stream | None:
    rows = []
    for msg in fit.messages.get("record", []):
        ts = _as_utc(msg.get("timestamp"))
        if ts is None:
            continue
        row: dict[str, Any] = {"t": ts}
        for name, value in msg.items():
            if name in _SKIP_FIELDS or not _numeric(value):
                continue
            if name in _PREFERRED and _PREFERRED[name] in msg:
                continue
            channel = _CHANNELS.get(name, name)
            if channel in ("lat", "lon"):
                value = value * _SEMICIRCLE
            row[channel] = float(value)
        if "cadence" in row and _numeric(msg.get("fractional_cadence")):
            row["cadence"] += float(msg["fractional_cadence"])
        rows.append(row)
    if not rows:
        return None
    names = sorted({k for row in rows for k in row} - {"t"})
    columns: dict[str, list[Any]] = {"t": [row["t"] for row in rows]}
    for name in names:
        columns[name] = [row.get(name) for row in rows]
    return Stream(fit.fit_id, columns)


def _activity(
    fit: DecodedFit, session: dict, start: datetime, offset: int, local_date: date
) -> Activity:
    device = fit.first("file_id")
    known = {
        "start_time",
        "sport",
        "sub_sport",
        "total_elapsed_time",
        "total_timer_time",
        "total_distance",
        "total_ascent",
        "avg_heart_rate",
        "max_heart_rate",
        "avg_power",
        "normalized_power",
        "enhanced_avg_speed",
        "avg_speed",
        "total_calories",
        "total_training_effect",
        "total_anaerobic_training_effect",
        "timestamp",
    }
    return Activity(
        source_id=fit.fit_id,
        start_utc=start,
        tz_offset_s=offset,
        local_date=local_date,
        sport=_str(session.get("sport")) or "unknown",
        sub_sport=_str(session.get("sub_sport")),
        name=None,
        elapsed_s=_float(session.get("total_elapsed_time")),
        moving_s=_float(session.get("total_timer_time")),
        distance_m=_float(session.get("total_distance")),
        ascent_m=_float(session.get("total_ascent")),
        avg_hr=_float(session.get("avg_heart_rate")),
        max_hr=_float(session.get("max_heart_rate")),
        avg_power=_float(session.get("avg_power")),
        norm_power=_float(session.get("normalized_power")),
        avg_speed=_float(session.get("enhanced_avg_speed") or session.get("avg_speed")),
        calories=_float(session.get("total_calories")),
        aerobic_te=_float(session.get("total_training_effect")),
        anaerobic_te=_float(session.get("total_anaerobic_training_effect")),
        device=" ".join(
            str(v)
            for v in (
                device.get("manufacturer"),
                device.get("garmin_product") or device.get("product"),
            )
            if v is not None
        )
        or None,
        fit_id=fit.fit_id,
        extra=_jsonable({k: v for k, v in session.items() if k not in known}),
    )


def _laps(fit: DecodedFit) -> Iterator[ActivityLap]:
    for index, lap in enumerate(fit.messages.get("lap", [])):
        yield ActivityLap(
            activity_source_id=fit.fit_id,
            lap_index=index,
            start_utc=_as_utc(lap.get("start_time")),
            elapsed_s=_float(lap.get("total_elapsed_time")),
            moving_s=_float(lap.get("total_timer_time")),
            distance_m=_float(lap.get("total_distance")),
            avg_hr=_float(lap.get("avg_heart_rate")),
            max_hr=_float(lap.get("max_heart_rate")),
            avg_power=_float(lap.get("avg_power")),
            avg_speed=_float(lap.get("enhanced_avg_speed") or lap.get("avg_speed")),
        )


def _zones(fit: DecodedFit, valid_from: date) -> Iterator[ZoneSet]:
    """The zones and thresholds stored in the file: what the device used that day."""
    target = fit.first("zones_target")
    sport = _str(fit.first("session").get("sport")) or "default"
    hr_highs = [z["high_bpm"] for z in fit.messages.get("hr_zone", []) if "high_bpm" in z]
    power_highs = [z["high_value"] for z in fit.messages.get("power_zone", []) if "high_value" in z]
    # Real device-configured pace (speed, m/s) zones, from ``speed_zone`` messages; the
    # device's own boundaries, never derived from heart rate or power.
    pace_highs = [z["high_value"] for z in fit.messages.get("speed_zone", []) if "high_value" in z]
    if hr_highs:
        yield ZoneSet(
            kind="hr",
            sport=sport,
            valid_from=valid_from,
            boundaries=tuple(float(v) for v in hr_highs),
            threshold=_float(target.get("threshold_heart_rate")),
            max_value=_float(target.get("max_heart_rate")),
            extra={"from": "fit", "boundary": "zone_high"},
        )
    if power_highs:
        yield ZoneSet(
            kind="power",
            sport=sport,
            valid_from=valid_from,
            boundaries=tuple(float(v) for v in power_highs),
            threshold=_float(target.get("functional_threshold_power")),
            extra={"from": "fit", "boundary": "zone_high"},
        )
    if pace_highs:
        yield ZoneSet(
            kind="pace",
            sport=sport,
            valid_from=valid_from,
            boundaries=tuple(float(v) for v in pace_highs),
            extra={"from": "fit", "boundary": "zone_high"},
        )


def _pace_zone_times(fit: DecodedFit, activity_id: str) -> Iterator[ActivityZoneTime]:
    """Recorded time per pace zone, read from the device's own session zone summary (FIT
    ``time_in_zone``, message 216, speed fields). Garmin Connect's and Strava's REST APIs
    have no pace-zone-time endpoint; this is the device's own record, never derived from
    heart rate or power. Absent on files/devices that don't write this message."""
    for msg in fit.messages.get("time_in_zone", []):
        if msg.get("reference_mesg") not in (None, "session"):
            continue  # per-lap entries use the same message; only the whole activity here
        times = msg.get("time_in_speed_zone")
        if not isinstance(times, tuple | list) or not times:
            continue
        highs = msg.get("speed_zone_high_boundary")
        if not isinstance(highs, tuple | list):
            highs = [
                zone["high_value"]
                for zone in fit.messages.get("speed_zone", [])
                if _numeric(zone.get("high_value"))
            ]
        for zone, seconds in enumerate(times):
            if not _numeric(seconds):
                continue
            if zone > 0 and zone - 1 < len(highs) and _numeric(highs[zone - 1]):
                low = float(highs[zone - 1])  # the previous zone's ceiling is this zone's floor
            else:
                low = 0.0 if zone == 0 else None
            yield ActivityZoneTime(activity_id, "pace", zone, float(seconds), low)


def _wellness(fit: DecodedFit) -> WellnessSamples | None:
    ts: list[datetime] = []
    metric: list[str] = []
    value: list[float] = []

    def add(when: datetime | None, name: str, v: Any) -> None:
        if when is not None and _numeric(v):
            ts.append(when)
            metric.append(name)
            value.append(float(v))

    last_full: int | None = None  # seconds since the FIT epoch
    for msg in fit.messages.get("monitoring", []):
        when = _as_utc(msg.get("timestamp"))
        if when is not None:
            last_full = int(when.timestamp()) - FIT_EPOCH
        elif last_full is not None and isinstance(msg.get("timestamp_16"), int):
            last_full += (msg["timestamp_16"] - (last_full & 0xFFFF)) & 0xFFFF
            when = datetime.fromtimestamp(last_full + FIT_EPOCH, UTC)
        if msg.get("heart_rate"):
            add(when, "hr.sample", msg["heart_rate"])
        if "steps" in msg:
            add(when, f"steps.cumulative.{msg.get('activity_type', 'unknown')}", msg["steps"])
        if "active_calories" in msg:
            add(when, "calories.active_cumulative", msg["active_calories"])
    for msg in fit.messages.get("stress_level", []):
        level = msg.get("stress_level_value")
        if _numeric(level) and level >= 0:  # negative values mean "could not measure"
            add(_as_utc(msg.get("stress_level_time")), "stress.sample", level)
    for msg in fit.messages.get("respiration_rate", []):
        rate = msg.get("respiration_rate")
        if _numeric(rate) and rate > 0:
            add(_as_utc(msg.get("timestamp")), "resp.sample", rate)
    if not ts:
        return None
    return WellnessSamples(fit.fit_id, ts, metric, value)


def _tz_offset(fit: DecodedFit, start: datetime | None, timezone: str) -> int:
    """Seconds east of UTC: from the file's activity message, else the configured zone."""
    act = fit.first("activity")
    local, utc = act.get("local_timestamp"), act.get("timestamp")
    if isinstance(local, datetime) and isinstance(utc, datetime):
        naive_local = local.replace(tzinfo=None)
        naive_utc = utc.astimezone(UTC).replace(tzinfo=None)
        offset = round((naive_local - naive_utc).total_seconds() / 900) * 900
        if abs(offset) <= 14 * 3600:
            return offset
    if start is None:
        return 0
    delta = start.astimezone(ZoneInfo(timezone)).utcoffset()
    return int(delta.total_seconds()) if delta else 0


def _first_timestamp(fit: DecodedFit, name: str) -> datetime | None:
    for msg in fit.messages.get(name, []):
        if ts := _as_utc(msg.get("timestamp")):
            return ts
    return None


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _numeric(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _float(value: Any) -> float | None:
    return float(value) if _numeric(value) else None


def _int(value: Any) -> int | None:
    return int(value) if _numeric(value) else None


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _jsonable(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, str | int | float | bool) or value is None:
            out[key] = value
        elif isinstance(value, tuple | list):
            out[key] = [v if isinstance(v, str | int | float | bool) else str(v) for v in value]
        else:
            out[key] = str(value)
    return out
