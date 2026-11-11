"""Withings raw items -> canonical records.

Measures: every measure type is stored in ``body_measurement``; types without a catalogue
metric are kept as ``withings.meastype_<n>`` so nothing is lost. Values arrive as
``value * 10**unit`` and are stored in SI (kg, m, s) or named units.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.model import (
    Activity,
    BodyMeasurement,
    DailyMetric,
    Record,
    SleepSession,
    SleepStage,
    WellnessSamples,
)
from vibesynergy.sources.base import Mapper
from vibesynergy.store.raw import RawRecord

# meastype -> (metric, unit)
MEASTYPES: dict[int, tuple[str, str]] = {
    1: ("body.weight", "kg"),
    4: ("body.height", "m"),
    5: ("body.lean_mass", "kg"),
    6: ("body.fat_pct", "pct"),
    8: ("body.fat_mass", "kg"),
    9: ("bp.diastolic", "mmHg"),
    10: ("bp.systolic", "mmHg"),
    11: ("hr.spot", "bpm"),  # bp.pulse when taken with a blood pressure reading
    12: ("body.temperature", "degC"),
    54: ("spo2.spot", "pct"),
    71: ("body.temperature", "degC"),
    73: ("body.skin_temp", "degC"),
    76: ("body.muscle_mass", "kg"),
    77: ("body.water_mass", "kg"),
    88: ("body.bone_mass", "kg"),
    91: ("body.pwv", "m_s"),
    123: ("fitness.vo2max.withings", "ml_kg_min"),
    130: ("heart.afib_ecg", "code"),
    135: ("heart.qrs_interval", "ms"),
    136: ("heart.pr_interval", "ms"),
    137: ("heart.qt_interval", "ms"),
    138: ("heart.qtc_interval", "ms"),
    155: ("body.vascular_age", "years"),
    167: ("body.nerve_health", "score"),
    168: ("body.extracellular_water", "kg"),
    169: ("body.intracellular_water", "kg"),
    170: ("body.visceral_fat", "score"),
    226: ("body.bmr", "kcal"),
    227: ("body.metabolic_age", "years"),
}

# Workout categories (Withings) -> sport; others become "withings_<n>".
WORKOUT_SPORTS = {
    1: "walking",
    2: "running",
    3: "hiking",
    6: "cycling",
    7: "swimming",
    16: "strength_training",
    18: "elliptical",
    19: "pilates",
    28: "yoga",
    187: "rowing",
    306: "indoor_walking",
    307: "treadmill_running",
    308: "indoor_cycling",
}

ACTIVITY = [
    ("steps", "daily.steps", "count", 1),
    ("distance", "daily.distance", "m", 1),
    ("elevation", "daily.elevation", "m", 1),
    ("calories", "daily.active_calories", "kcal", 1),
    ("totalcalories", "daily.total_calories", "kcal", 1),
    ("moderate", "daily.intensity_minutes.moderate", "min", 1 / 60),
    ("intense", "daily.intensity_minutes.vigorous", "min", 1 / 60),
    ("hr_average", "hr.avg", "bpm", 1),
    ("hr_min", "hr.min", "bpm", 1),
    ("hr_max", "hr.max", "bpm", 1),
]

SLEEP = [
    ("sleep_score", "sleep.score", "score", 1),
    ("total_sleep_time", "sleep.duration", "s", 1),
    ("deepsleepduration", "sleep.deep", "s", 1),
    ("lightsleepduration", "sleep.light", "s", 1),
    ("remsleepduration", "sleep.rem", "s", 1),
    ("wakeupduration", "sleep.awake", "s", 1),
    ("sleep_efficiency", "sleep.efficiency", "pct", 100),
    ("sleep_latency", "sleep.latency", "s", 1),
    ("hr_average", "sleep.avg_hr", "bpm", 1),
    ("rr_average", "sleep.avg_respiration", "brpm", 1),
    ("snoring", "sleep.snoring", "s", 1),
    ("apnea_hypopnea_index", "sleep.apnea_hypopnea_index", "per_h", 1),
    ("wakeupcount", "sleep.wakeup_count", "count", 1),
]

_SLEEP_STATES = {0: "awake", 1: "light", 2: "deep", 3: "rem"}


def build(timezone: str) -> dict[str, Mapper]:
    def measures(raw: RawRecord) -> Iterable[Record]:
        return map_measures(raw, timezone)

    def workouts(raw: RawRecord) -> Iterable[Record]:
        return map_workouts(raw, timezone)

    def heart(raw: RawRecord) -> Iterable[Record]:
        return map_heart(raw, timezone)

    return {
        "measures": measures,
        "activity": map_activity,
        "intraday_activity": map_intraday,
        "workouts": workouts,
        "sleep_summary": map_sleep_summary,
        "sleep_series": map_sleep_series,
        "heart": heart,
    }


def map_measures(raw: RawRecord, timezone: str) -> Iterator[Record]:
    body = raw.json() or {}
    tz = ZoneInfo(body.get("timezone") or timezone)
    last: dict[tuple[date, str], DailyMetric] = {}
    for grp in sorted(body.get("measuregrps") or [], key=lambda g: g.get("date", 0)):
        if grp.get("category", 1) != 1:  # 2 = user objectives, not measurements
            continue
        measured = datetime.fromtimestamp(grp["date"], UTC)
        day = measured.astimezone(tz).date()
        types = {m.get("type") for m in grp.get("measures") or []}
        is_bp = bool(types & {9, 10})
        extra = {k: grp.get(k) for k in ("deviceid", "model", "attrib") if grp.get(k) is not None}
        for m in grp.get("measures") or []:
            mtype, value = m.get("type"), m.get("value")
            if mtype is None or value is None:
                continue
            value = value * 10 ** int(m.get("unit", 0))
            metric, unit = MEASTYPES.get(mtype, (f"withings.meastype_{mtype}", "raw"))
            if mtype == 11 and is_bp:
                metric = "bp.pulse"
            yield BodyMeasurement(str(grp["grpid"]), measured, metric, value, unit, extra)
            if mtype in MEASTYPES:
                last[(day, metric)] = DailyMetric(day, metric, value, unit)
    yield from last.values()


def map_activity(raw: RawRecord) -> Iterator[Record]:
    for entry in (raw.json() or {}).get("activities") or []:
        day = _date(entry.get("date"))
        if not day:
            continue
        for key, metric, unit, factor in ACTIVITY:
            value = _num(entry.get(key))
            if value is not None:
                yield DailyMetric(day, metric, value * factor, unit)


def map_intraday(raw: RawRecord) -> Iterator[Record]:
    series = (raw.json() or {}).get("series") or {}
    ts: list[datetime] = []
    metric: list[str] = []
    value: list[float] = []
    names = {
        "heart_rate": "hr.sample",
        "steps": "steps.sample",
        "spo2_auto": "spo2.sample",
        "calories": "calories.sample",
        "distance": "distance.sample",
    }
    for stamp, sample in sorted(series.items(), key=lambda kv: int(kv[0])):
        when = datetime.fromtimestamp(int(stamp), UTC)
        for key, name in names.items():
            v = _num(sample.get(key))
            if v is not None:
                ts.append(when)
                metric.append(name)
                value.append(v)
    if ts:
        yield WellnessSamples(f"intraday-{raw.item_key}", ts, metric, value)


def map_workouts(raw: RawRecord, timezone: str) -> Iterator[Record]:
    for w in (raw.json() or {}).get("series") or []:
        if w.get("id") is None or w.get("startdate") is None:
            continue
        tz = ZoneInfo(w.get("timezone") or timezone)
        start = datetime.fromtimestamp(w["startdate"], UTC)
        offset = start.astimezone(tz).utcoffset()
        data = w.get("data") or {}
        category = w.get("category")
        yield Activity(
            source_id=str(w["id"]),
            start_utc=start,
            tz_offset_s=int(offset.total_seconds()) if offset else 0,
            local_date=start.astimezone(tz).date(),
            sport=WORKOUT_SPORTS.get(category, f"withings_{category}"),
            elapsed_s=float(w["enddate"] - w["startdate"]) if w.get("enddate") else None,
            distance_m=_num(data.get("distance")) or _num(data.get("manual_distance")),
            ascent_m=_num(data.get("elevation")),
            avg_hr=_num(data.get("hr_average")),
            max_hr=_num(data.get("hr_max")),
            calories=_num(data.get("calories")) or _num(data.get("manual_calories")),
            device=str(w["model"]) if w.get("model") is not None else None,
            extra={"category": category, **data},
        )


def map_sleep_summary(raw: RawRecord) -> Iterator[Record]:
    for night in (raw.json() or {}).get("series") or []:
        day = _date(night.get("date"))
        if not day:
            continue
        data = night.get("data") or {}
        for key, metric, unit, factor in SLEEP:
            value = _num(data.get(key))
            if value is not None:
                yield DailyMetric(day, metric, value * factor, unit)
        yield SleepSession(
            source_id=str(night.get("id") or day.isoformat()),
            local_date=day,
            start_utc=_epoch(night.get("startdate")),
            end_utc=_epoch(night.get("enddate")),
            duration_s=_num(data.get("total_sleep_time")),
            deep_s=_num(data.get("deepsleepduration")),
            light_s=_num(data.get("lightsleepduration")),
            rem_s=_num(data.get("remsleepduration")),
            awake_s=_num(data.get("wakeupduration")),
            score=_num(data.get("sleep_score")),
            extra={"model": night.get("model"), **data},
        )


def map_sleep_series(raw: RawRecord) -> Iterator[Record]:
    """Stages for the night ending on the item's day; per-minute HR/RR/snoring samples."""
    session = f"night-{raw.item_key}"
    ts: list[datetime] = []
    metric: list[str] = []
    value: list[float] = []
    for seg in (raw.json() or {}).get("series") or []:
        start, end = _epoch(seg.get("startdate")), _epoch(seg.get("enddate"))
        stage = _SLEEP_STATES.get(seg.get("state"))
        if start and end and stage:
            yield SleepStage(session, start, end, stage)
        for key, name in (
            ("hr", "sleep.hr_sample"),
            ("rr", "sleep.rr_sample"),
            ("snoring", "sleep.snoring_sample"),
            ("rmssd", "sleep.rmssd_sample"),
        ):
            for stamp, v in (seg.get(key) or {}).items():
                if _num(v) is not None:
                    ts.append(datetime.fromtimestamp(int(stamp), UTC))
                    metric.append(name)
                    value.append(float(v))
    if ts:
        yield WellnessSamples(f"sleep-{raw.item_key}", ts, metric, value)


def map_heart(raw: RawRecord, timezone: str) -> Iterator[Record]:
    for entry in (raw.json() or {}).get("series") or []:
        when = _epoch(entry.get("timestamp"))
        if when is None:
            continue
        reading = f"heart-{entry['timestamp']}"
        extra = {"model": entry.get("model"), "deviceid": entry.get("deviceid")}
        bp = entry.get("bloodpressure") or {}
        for key, metric, unit in (
            ("systole", "bp.systolic", "mmHg"),
            ("diastole", "bp.diastolic", "mmHg"),
        ):
            if _num(bp.get(key)) is not None:
                yield BodyMeasurement(reading, when, metric, float(bp[key]), unit, extra)
        if _num(entry.get("heart_rate")) is not None:
            metric = "bp.pulse" if bp else "hr.spot"
            yield BodyMeasurement(reading, when, metric, float(entry["heart_rate"]), "bpm", extra)
        ecg = entry.get("ecg") or {}
        if _num(ecg.get("afib")) is not None:
            yield BodyMeasurement(
                reading,
                when,
                "heart.afib_ecg",
                float(ecg["afib"]),
                "code",
                {**extra, "signalid": ecg.get("signalid")},
            )


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _epoch(value: Any) -> datetime | None:
    number = _num(value)
    return datetime.fromtimestamp(number, UTC) if number else None
