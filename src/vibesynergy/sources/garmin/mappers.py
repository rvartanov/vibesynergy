"""Garmin raw items -> canonical records.

Only payload shapes that are well known are mapped; every other dataset stays in the raw
archive until a mapper is written for it (normalisation can catch up, collection cannot).
Field names follow the Garmin Connect JSON returned by ``garminconnect``. Mappers are
defensive: a missing field means no value, never an error.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.fit.decode import fit_files_in, map_fit
from vibesynergy.model import (
    Activity,
    ActivityLap,
    ActivityZoneTime,
    BodyMeasurement,
    CalendarSpan,
    DailyMetric,
    PlannedItem,
    Record,
    SleepSession,
    SleepStage,
    TrainingPlan,
    WellnessSamples,
    ZoneSet,
)
from vibesynergy.sources.base import Mapper
from vibesynergy.sources.garmin.calendar_identity import native_calendar_key
from vibesynergy.store.raw import RawRecord

# (Garmin field, metric id, unit, factor)
USER_SUMMARY = [
    ("totalSteps", "daily.steps", "count", 1),
    ("floorsAscended", "daily.floors", "count", 1),
    ("totalDistanceMeters", "daily.distance", "m", 1),
    ("activeKilocalories", "daily.active_calories", "kcal", 1),
    ("totalKilocalories", "daily.total_calories", "kcal", 1),
    ("moderateIntensityMinutes", "daily.intensity_minutes.moderate", "min", 1),
    ("vigorousIntensityMinutes", "daily.intensity_minutes.vigorous", "min", 1),
    ("restingHeartRate", "hr.resting", "bpm", 1),
    ("minHeartRate", "hr.min", "bpm", 1),
    ("maxHeartRate", "hr.max", "bpm", 1),
    ("averageStressLevel", "recovery.stress.avg", "score", 1),
    ("restStressDuration", "recovery.stress.rest_minutes", "min", 1 / 60),
    ("highStressDuration", "recovery.stress.high_minutes", "min", 1 / 60),
    ("bodyBatteryHighestValue", "recovery.body_battery.max", "score", 1),
    ("bodyBatteryLowestValue", "recovery.body_battery.min", "score", 1),
    ("bodyBatteryChargedValue", "recovery.body_battery.charged", "score", 1),
    ("bodyBatteryDrainedValue", "recovery.body_battery.drained", "score", 1),
    ("averageSpo2", "spo2.avg", "pct", 1),
    ("lowestSpo2", "spo2.min", "pct", 1),
    ("avgWakingRespirationValue", "resp.waking_avg", "brpm", 1),
]

SLEEP_DTO = [
    ("sleepTimeSeconds", "sleep.duration", "s", 1),
    ("deepSleepSeconds", "sleep.deep", "s", 1),
    ("lightSleepSeconds", "sleep.light", "s", 1),
    ("remSleepSeconds", "sleep.rem", "s", 1),
    ("awakeSleepSeconds", "sleep.awake", "s", 1),
    ("averageSpO2Value", "sleep.spo2_avg", "pct", 1),
    ("averageRespirationValue", "sleep.avg_respiration", "brpm", 1),
    ("avgSleepStress", "sleep.avg_stress", "score", 1),
    ("avgHeartRate", "sleep.avg_hr", "bpm", 1),
]

# Body composition: (Garmin field, metric id, unit, factor). Masses arrive in grams.
BODY_COMPOSITION = [
    ("weight", "body.weight", "kg", 1 / 1000),
    ("bmi", "body.bmi", "kg_m2", 1),
    ("bodyFat", "body.fat_pct", "pct", 1),
    ("bodyWater", "body.water_pct", "pct", 1),
    ("boneMass", "body.bone_mass", "kg", 1 / 1000),
    ("muscleMass", "body.muscle_mass", "kg", 1 / 1000),
    ("visceralFat", "body.visceral_fat", "score", 1),
]

RACE_PREDICTIONS = [
    ("time5K", "fitness.race_pred.5k"),
    ("time10K", "fitness.race_pred.10k"),
    ("timeHalfMarathon", "fitness.race_pred.half"),
    ("timeMarathon", "fitness.race_pred.marathon"),
]

_SLEEP_LEVELS = {0: "deep", 1: "light", 2: "rem", 3: "awake"}
_ZONE_FLOOR = re.compile(r"^zone(\d+)Floor$")


# Sports without travel: Garmin's movingDuration counts only the time it saw an exercise set
# (a 59-minute pilates class showed as 11 minutes), so the session's own time
# is its elapsed time.
STATIONARY = (
    "strength", "pilates", "yoga", "breathwork", "mobility", "flexibility", "hiit",
    "indoor_cardio", "fitness_equipment", "meditation", "stretching",
)  # fmt: skip


def moving_seconds(sport, moving, elapsed):
    """Garmin's moving time, or the elapsed time for a stationary sport."""
    if elapsed and any(word in str(sport or "").lower() for word in STATIONARY):
        return elapsed
    return moving


def build(timezone: str) -> dict[str, Mapper]:
    """Mappers bound to the athlete's time zone (for dates Garmin gives only in UTC)."""

    def activity_files(raw: RawRecord) -> Iterable[Record]:
        activity_id = raw.item_key
        for _, data in fit_files_in(raw.payload):
            yield from map_fit(data, timezone=timezone, source_activity_id=activity_id)

    def wellness_files(raw: RawRecord) -> Iterable[Record]:
        for _, data in fit_files_in(raw.payload):
            yield from map_fit(data, timezone=timezone)

    def zones(raw: RawRecord) -> Iterable[Record]:
        return map_zone_snapshot(raw, timezone)

    def calendar(raw: RawRecord) -> Iterable[Record]:
        return map_calendar(raw, timezone)

    def profile(raw: RawRecord) -> Iterable[Record]:
        return map_profile_threshold(raw, timezone)

    return {
        "user_summary": map_user_summary,
        "profile": profile,
        "sleep": map_sleep,
        "hrv": map_hrv,
        "heart_rate": map_heart_rate,
        "stress": map_stress,
        "training_readiness": map_training_readiness,
        "max_metrics": map_max_metrics,
        "fitness_age": map_fitness_age,
        "hydration": map_hydration,
        "body_composition": map_body_composition,
        "blood_pressure": map_blood_pressure,
        "race_predictions": map_race_predictions,
        "scheduled_workouts": calendar,
        "training_plans": map_training_plans,
        "heart_rate_zones": zones,
        "power_zones": zones,
        "critical_swim_speed": map_critical_swim_speed,
        "activities": map_activity,
        "activity_files": activity_files,
        "wellness_files": wellness_files,
    }


# Daily wellness ---------------------------------------------------------------------------


def metrics_from(
    data: dict, day: date, fields: list[tuple[str, str, str, float]]
) -> Iterator[DailyMetric]:
    for key, metric, unit, factor in fields:
        value = num(data.get(key))
        if value is not None:
            yield DailyMetric(day, metric, value * factor, unit)


def map_user_summary(raw: RawRecord) -> Iterable[Record]:
    data = raw.json() or {}
    yield from metrics_from(data, _day(raw), USER_SUMMARY)


def map_sleep(raw: RawRecord) -> Iterable[Record]:
    data = raw.json() or {}
    dto = data.get("dailySleepDTO") or {}
    if not dto.get("sleepTimeSeconds"):
        return  # no sleep recorded that night
    day = _day(raw)
    yield from metrics_from(dto, day, SLEEP_DTO)
    score = num(((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
    if score is not None:
        yield DailyMetric(day, "sleep.score", score, "score")
    for key, metric, unit in (
        ("avgOvernightHrv", "sleep.avg_hrv", "ms"),
        ("restlessMomentsCount", "sleep.restless_moments", "count"),
        ("restingHeartRate", "sleep.resting_hr", "bpm"),
        ("avgSkinTempDeviationC", "sleep.skin_temp_deviation", "degC"),
        ("bodyBatteryChange", "sleep.body_battery_change", "score"),
    ):
        if (value := num(data.get(key))) is not None:
            yield DailyMetric(day, metric, value, unit)

    session_id = str(dto.get("id") or day.isoformat())
    known = {key for key, *_ in SLEEP_DTO} | {"sleepScores", "id", "calendarDate"}
    yield SleepSession(
        source_id=session_id,
        local_date=day,
        start_utc=_from_ms(dto.get("sleepStartTimestampGMT")),
        end_utc=_from_ms(dto.get("sleepEndTimestampGMT")),
        duration_s=num(dto.get("sleepTimeSeconds")),
        deep_s=num(dto.get("deepSleepSeconds")),
        light_s=num(dto.get("lightSleepSeconds")),
        rem_s=num(dto.get("remSleepSeconds")),
        awake_s=num(dto.get("awakeSleepSeconds")),
        score=score,
        extra={k: v for k, v in dto.items() if k not in known},
    )
    for level in data.get("sleepLevels") or []:
        start, end = _gmt(level.get("startGMT")), _gmt(level.get("endGMT"))
        code = num(level.get("activityLevel"))
        stage = _SLEEP_LEVELS.get(int(code)) if code is not None else None
        if start and end and stage:
            yield SleepStage(session_id, start, end, stage)


def map_hrv(raw: RawRecord) -> Iterable[Record]:
    summary = (raw.json() or {}).get("hrvSummary") or {}
    day = _day(raw)
    baseline = summary.get("baseline") or {}
    for value, metric in (
        (summary.get("lastNightAvg"), "hrv.night_avg"),
        (summary.get("weeklyAvg"), "hrv.weekly_avg"),
        (summary.get("lastNight5MinHigh"), "hrv.night_5min_high"),
        (baseline.get("balancedLow"), "hrv.baseline_low"),
        (baseline.get("balancedUpper"), "hrv.baseline_high"),
    ):
        if (v := num(value)) is not None:
            yield DailyMetric(day, metric, v, "ms")


# Intraday (all-day) samples ----------------------------------------------------------------


def _samples(
    rows: Any, metric: str, value_index: int, ts: list, metrics: list, values: list
) -> None:
    """Append ``[epoch_ms, …, value, …]`` rows; negative values mean "not measured"."""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list | tuple) or len(row) <= value_index:
            continue
        when, value = _from_ms(row[0]), num(row[value_index])
        if when is not None and value is not None and value >= 0:
            ts.append(when)
            metrics.append(metric)
            values.append(value)


def map_heart_rate(raw: RawRecord) -> Iterable[Record]:
    """All-day heart rate, every 2 minutes: ``heartRateValues`` = [[epoch_ms, bpm], …]."""
    data = raw.json() or {}
    ts: list[datetime] = []
    metrics: list[str] = []
    values: list[float] = []
    _samples(data.get("heartRateValues"), "hr.sample", 1, ts, metrics, values)
    if ts:
        yield WellnessSamples(f"heart_rate-{raw.item_key}", ts, metrics, values)


def map_stress(raw: RawRecord) -> Iterable[Record]:
    """All-day stress and Body Battery, every 3 minutes: ``stressValuesArray`` =
    [[epoch_ms, level], …] and ``bodyBatteryValuesArray`` = [[epoch_ms, status, level, …]]."""
    data = raw.json() or {}
    ts: list[datetime] = []
    metrics: list[str] = []
    values: list[float] = []
    _samples(data.get("stressValuesArray"), "stress.sample", 1, ts, metrics, values)
    _samples(data.get("bodyBatteryValuesArray"), "bodybattery.sample", 2, ts, metrics, values)
    if ts:
        yield WellnessSamples(f"stress-{raw.item_key}", ts, metrics, values)


def map_training_readiness(raw: RawRecord) -> Iterable[Record]:
    entries = _as_list(raw.json())
    scored = [e for e in entries if num(e.get("score")) is not None]
    if scored:  # several readings a day; the latest counts
        latest = max(scored, key=lambda e: str(e.get("timestamp") or ""))
        yield DailyMetric(_day(raw), "recovery.readiness", num(latest["score"]), "score")


def map_max_metrics(raw: RawRecord) -> Iterable[Record]:
    day = _day(raw)
    for entry in _as_list(raw.json()):
        for key, metric in (("generic", "fitness.vo2max.run"), ("cycling", "fitness.vo2max.ride")):
            block = entry.get(key) or {}
            value = num(block.get("vo2MaxPreciseValue")) or num(block.get("vo2MaxValue"))
            if value is not None:
                yield DailyMetric(day, metric, value, "ml_kg_min")


def map_fitness_age(raw: RawRecord) -> Iterable[Record]:
    value = num((raw.json() or {}).get("fitnessAge"))
    if value is not None:
        yield DailyMetric(_day(raw), "fitness.fitness_age", value, "years")


def map_hydration(raw: RawRecord) -> Iterable[Record]:
    value = num((raw.json() or {}).get("valueInML"))
    if value:
        yield DailyMetric(_day(raw), "nutrition.hydration", value, "ml")


# Body -------------------------------------------------------------------------------------


def map_body_composition(raw: RawRecord) -> Iterable[Record]:
    entries = (raw.json() or {}).get("dateWeightList") or []
    last_per_day: dict[tuple[date, str], DailyMetric] = {}

    # "date" is midnight of the calendar day; "timestampGMT" is when the weigh-in happened.
    def measured_at(entry: dict) -> float:
        return num(entry.get("timestampGMT")) or num(entry.get("date")) or 0

    for entry in sorted(entries, key=measured_at):
        measured = _from_ms(measured_at(entry))
        day_text = entry.get("calendarDate")
        if measured is None or not day_text:
            continue
        day = date.fromisoformat(day_text[:10])
        entry_id = str(entry.get("samplePk") or entry.get("date"))
        extra = {k: entry.get(k) for k in ("sourceType", "weightDelta") if k in entry}
        for key, metric, unit, factor in BODY_COMPOSITION:
            value = num(entry.get(key))
            if value is None:
                continue
            yield BodyMeasurement(entry_id, measured, metric, value * factor, unit, extra)
            last_per_day[(day, metric)] = DailyMetric(day, metric, value * factor, unit)
    yield from last_per_day.values()


def map_blood_pressure(raw: RawRecord) -> Iterable[Record]:
    data = raw.json() or {}
    readings = [
        m
        for summary in data.get("measurementSummaries") or []
        for m in summary.get("measurements") or []
    ]
    last_per_day: dict[tuple[date, str], DailyMetric] = {}
    for reading in sorted(readings, key=lambda m: str(m.get("measurementTimestampGMT"))):
        measured = _gmt(reading.get("measurementTimestampGMT"))
        local = reading.get("measurementTimestampLocal")
        if measured is None:
            continue
        day = date.fromisoformat(local[:10]) if local else measured.date()
        reading_id = str(reading.get("version") or reading.get("measurementTimestampGMT"))
        for key, metric, unit in (
            ("systolic", "bp.systolic", "mmHg"),
            ("diastolic", "bp.diastolic", "mmHg"),
            ("pulse", "bp.pulse", "bpm"),
        ):
            value = num(reading.get(key))
            if value is not None:
                yield BodyMeasurement(reading_id, measured, metric, value, unit)
                last_per_day[(day, metric)] = DailyMetric(day, metric, value, unit)
    yield from last_per_day.values()


# Performance --------------------------------------------------------------------------------


def map_race_predictions(raw: RawRecord) -> Iterable[Record]:
    for entry in _as_list(raw.json()):
        day_text = entry.get("calendarDate")
        if not day_text:
            continue
        day = date.fromisoformat(str(day_text)[:10])
        for key, metric in RACE_PREDICTIONS:
            if (value := num(entry.get(key))) is not None:
                yield DailyMetric(day, metric, value, "s")


# Plans and the calendar -------------------------------------------------------------------

# Calendar item types kept: what is planned or entered, not what happened (activities, weigh-
# ins and naps are on the calendar too, and arrive through their own datasets).
_MONTH_KEY = re.compile(r"\d{4}-\d{2}")
_CALENDAR_KINDS = {"fbtAdaptiveWorkout": "plan_workout", "workout": "workout", "event": "event"}
_CALENDAR_EXTRA = (
    "location",
    "completionTarget",
    "eventTimeLocal",
    "workoutId",
    "workoutUuid",
    "courseId",
    "tpType",
    "shareableEventUuid",
    "duration",
    "distance",
)


def validate_calendar(reply):
    """A missing or malformed month is not evidence of an empty schedule."""
    if not isinstance(reply, dict) or not isinstance(reply.get("calendarItems"), list):
        raise ValueError("Incomplete Garmin calendar response")
    for item in reply["calendarItems"]:
        if not isinstance(item, dict):
            raise ValueError("Malformed Garmin calendar item")
        if item.get("itemType") in _CALENDAR_KINDS:
            date.fromisoformat(str(item.get("date", ""))[:10])
    return reply


def map_calendar(raw: RawRecord, timezone: str = "UTC") -> Iterable[Record]:
    """One month of the Garmin calendar: coaching-plan workouts, scheduled workouts, events.

    Garmin returns the active plan's workouts in every month's reply (with fresh ids each
    time), and months overlap at their edges, so only entries dated in this item's own month
    are kept; each day then comes from exactly one month.

    Garmin also leaves past plan days out of its replies, so earlier versions are kept for
    planned vs done. From the day it was fetched on, a CalendarSpan reconciles the month;
    normalisation guards against a reply omitting every workout of an active adaptive plan."""
    month = raw.item_key if _MONTH_KEY.fullmatch(raw.item_key) else None
    reply = validate_calendar(raw.json())
    if month and "calendarItems" in reply:  # not for an empty or failed reply
        first = date.fromisoformat(f"{month}-01")
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        fetched = raw.fetched_at.astimezone(ZoneInfo(timezone)).date()
        if fetched <= last:
            yield CalendarSpan(max(first, fetched), last)
    seen: set[str] = set()
    for item in reply.get("calendarItems") or []:
        kind = _CALENDAR_KINDS.get(item.get("itemType"))
        day_text = item.get("date")
        if not kind or not day_text or (month and not str(day_text).startswith(month)):
            continue
        plan_id = item.get("trainingPlanId")
        if kind == "plan_workout":
            # Garmin issues new ids for plan workouts on every reply and adapts them; one per
            # plan, day and sport keeps the latest version of each session.
            source_id = f"{kind}:{plan_id}:{day_text}:{item.get('sportTypeKey')}"
        else:
            ident = item.get("shareableEventUuid") or item.get("id")
            source_id = f"{kind}:{ident}" if ident else f"{kind}:{day_text}:{item.get('title')}"
        if source_id in seen:  # Garmin lists some events twice
            continue
        seen.add(source_id)
        yield PlannedItem(
            source_id,
            date.fromisoformat(str(day_text)[:10]),
            kind,
            title=item.get("title"),
            sport=item.get("sportTypeKey"),
            plan_id=str(plan_id) if plan_id else None,
            is_race=bool(item.get("isRace")),
            primary=bool(item.get("primaryEvent")),
            extra={
                **{k: item[k] for k in _CALENDAR_EXTRA if item.get(k) not in (None, "", [], {})},
                "native_calendar_key": native_calendar_key(item),
            },
        )


def map_training_plans(raw: RawRecord) -> Iterable[Record]:
    for plan in (raw.json() or {}).get("trainingPlanList") or []:
        if not plan.get("trainingPlanId") or not plan.get("name"):
            continue

        yield TrainingPlan(
            str(plan["trainingPlanId"]),
            plan["name"],
            start_date=_plan_day(plan.get("startDate")),
            end_date=_plan_day(plan.get("endDate")),
            status=(plan.get("trainingStatus") or {}).get("statusKey"),
            plan_type=(plan.get("trainingSubType") or {}).get("subTypeKey"),
            level=(plan.get("trainingLevel") or {}).get("levelKey"),
            extra={
                k: plan[k]
                for k in (
                    "description",
                    "durationInWeeks",
                    "avgWeeklyWorkouts",
                    "trainingPlanCategory",
                )
                if plan.get(k) is not None
            },
        )


def _plan_day(text: str | None) -> date | None:
    return date.fromisoformat(str(text)[:10]) if text else None


def map_zone_snapshot(raw: RawRecord, timezone: str) -> Iterable[Record]:
    """HR or power zones as configured in Garmin Connect, valid from the day fetched."""
    kind = "hr" if raw.dataset == "heart_rate_zones" else "power"
    valid_from = raw.fetched_at.astimezone(ZoneInfo(timezone)).date()
    for entry in _as_list(raw.json()):
        floors = sorted(
            (int(m.group(1)), num(v))
            for k, v in entry.items()
            if (m := _ZONE_FLOOR.match(k)) and num(v) is not None
        )
        if not floors:
            continue
        boundaries = [v for _, v in floors]
        max_value = num(entry.get("maxHeartRateUsed"))
        if kind == "hr" and max_value:
            boundaries.append(max_value)
        threshold = num(entry.get("lactateThresholdHeartRateUsed")) or num(
            entry.get("functionalThresholdPower")
        )
        yield ZoneSet(
            kind=kind,
            sport=str(entry.get("sport") or "default").lower(),
            valid_from=valid_from,
            boundaries=tuple(boundaries),
            threshold=threshold,
            max_value=max_value,
            extra={
                "from": "garmin_connect",
                "boundary": "zone_low",
                **{k: v for k, v in entry.items() if not _ZONE_FLOOR.match(k)},
            },
        )


def map_profile_threshold(raw: RawRecord, timezone: str) -> Iterable[Record]:
    """The profile's lactate threshold (Performance stats) and auto-detection.

    Garmin keeps the threshold speed as seconds per metre; stored here as s/km."""
    payload = raw.json()
    profile = payload.get("user_profile") if isinstance(payload, dict) else None
    data = (profile or {}).get("userData") if isinstance(profile, dict) else None
    if not isinstance(data, dict):
        return
    lthr = num(data.get("lactateThresholdHeartRate"))
    speed = num(data.get("lactateThresholdSpeed"))
    if lthr is None and speed is None:
        return
    yield ZoneSet(
        kind="lt",
        sport="running",
        valid_from=raw.fetched_at.astimezone(ZoneInfo(timezone)).date(),
        boundaries=(),
        threshold=lthr,
        max_value=None,
        extra={
            "from": "garmin_profile",
            "pace_s_per_km": round(speed * 1000, 1) if speed else None,
            "auto_detect": bool(data.get("thresholdHeartRateAutoDetected"))
            or bool(data.get("ftpAutoDetected")),
        },
    )


def map_critical_swim_speed(raw: RawRecord) -> Iterable[Record]:
    """Garmin's latest critical swim speed (mm/s), valid from the day it was measured."""
    entry = raw.json()
    if not isinstance(entry, dict):
        return
    speed = num(entry.get("criticalSwimSpeed"))
    measured = str(entry.get("measurementTimestampLocal") or "")[:10]
    if not speed or not measured:
        return
    yield ZoneSet(
        kind="css",
        sport="swimming",
        valid_from=date.fromisoformat(measured),
        boundaries=(),
        threshold=speed,
        max_value=None,
        extra={"from": "garmin_connect", "unit": "mm/s", **entry},
    )


# Activities -------------------------------------------------------------------------------

_ACTIVITY_FIELDS = {
    "activityId",
    "activityName",
    "startTimeGMT",
    "startTimeLocal",
    "activityType",
    "elapsedDuration",
    "duration",
    "movingDuration",
    "distance",
    "elevationGain",
    "averageHR",
    "maxHR",
    "avgPower",
    "normPower",
    "averageSpeed",
    "calories",
    "activityTrainingLoad",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "deviceId",
}


def map_activity(raw: RawRecord) -> Iterable[Record]:
    data = raw.json()
    summary = data.get("summary") or {}
    activity_id = str(summary.get("activityId") or raw.item_key)
    start = _gmt(summary.get("startTimeGMT"))
    local = _local(summary.get("startTimeLocal"))
    if start is None:
        raise ValueError(f"activity {activity_id} has no startTimeGMT")
    offset = round((local - start.replace(tzinfo=None)).total_seconds()) if local else None
    type_info = summary.get("activityType") or {}
    extra = {k: v for k, v in summary.items() if k not in _ACTIVITY_FIELDS and v is not None}
    # How it felt and how hard it was, as entered on the watch or in Garmin Connect (0-100).
    detail = ((data.get("activity") or {}).get("summaryDTO")) or {}
    for key, name in (("directWorkoutFeel", "feel"), ("directWorkoutRpe", "rpe")):
        if num(detail.get(key)) is not None:
            extra[name] = num(detail.get(key))
    sets_reply = data.get("exercise_sets") or {}
    exercise_sets = sets_reply.get("exerciseSets") if isinstance(sets_reply, dict) else sets_reply
    if isinstance(exercise_sets, list):
        work_sets = [
            item
            for item in exercise_sets
            if isinstance(item, dict) and item.get("setType") != "REST"
        ]
        if work_sets:
            extra["sets"] = len(work_sets)
            counts = [num(item.get("repetitionCount")) for item in work_sets]
            if any(value is not None for value in counts):
                extra["repetitions"] = sum(value or 0 for value in counts)
            # Identity only (category + sub-category enum), matched against Garmin's own
            # catalog in query/workouts.py. set_activity_exercise_sets() documents this same
            # exercises[].category / exercises[].name shape for the write side.
            exercises, seen_pairs = [], set()
            for item in work_sets:
                for ex in _as_list(item.get("exercises")):
                    category = ex.get("category")
                    if not category:
                        continue
                    name = ex.get("name") or None
                    pair = (str(category), str(name) if name else None)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        exercises.append({"category": pair[0], "name": pair[1]})
            if exercises:
                extra["exercises"] = exercises
    # Gear used for this activity (same gear-service endpoint as the profile gear list,
    # filtered by activityId instead of userProfilePk, so items share that identity: uuid).
    gear_ids = sorted({str(g["uuid"]) for g in _as_list(data.get("gear")) if g.get("uuid")})
    if gear_ids:
        extra["gear_ids"] = gear_ids
    if data.get("errors"):
        extra["detail_errors"] = data["errors"]
    yield Activity(
        source_id=activity_id,
        start_utc=start,
        tz_offset_s=offset,
        local_date=(local or start).date(),
        sport=str(type_info.get("typeKey") or "unknown"),
        name=summary.get("activityName"),
        elapsed_s=num(summary.get("elapsedDuration")) or num(summary.get("duration")),
        moving_s=moving_seconds(
            type_info.get("typeKey"),
            num(summary.get("movingDuration")) or num(summary.get("duration")),
            num(summary.get("elapsedDuration")) or num(summary.get("duration")),
        ),
        distance_m=num(summary.get("distance")),
        ascent_m=num(summary.get("elevationGain")),
        avg_hr=num(summary.get("averageHR")),
        max_hr=num(summary.get("maxHR")),
        avg_power=num(summary.get("avgPower")),
        norm_power=num(summary.get("normPower")),
        avg_speed=num(summary.get("averageSpeed")),
        calories=num(summary.get("calories")),
        training_load=num(summary.get("activityTrainingLoad")),
        aerobic_te=num(summary.get("aerobicTrainingEffect")),
        anaerobic_te=num(summary.get("anaerobicTrainingEffect")),
        device=str(summary["deviceId"]) if summary.get("deviceId") else None,
        extra=extra,
    )
    laps = (data.get("splits") or {}).get("lapDTOs") or []
    if not laps:
        # Some Garmin sports only return sets in /typedsplits. Use the same lap
        # contract, leaving unfamiliar response shapes in the raw item.
        typed = data.get("typed_splits") or {}
        if isinstance(typed, dict):
            for key in ("swimSplits", "runSplits", "bikeSplits"):
                entries = typed.get(key) or []
                if isinstance(entries, dict):
                    entries = entries.get("lapDTOs") or []
                if isinstance(entries, list):
                    laps.extend(item for item in entries if isinstance(item, dict))
    for lap in laps:
        index = lap.get("lapIndex")
        if index is None:
            continue
        yield ActivityLap(
            activity_source_id=activity_id,
            lap_index=int(index),
            start_utc=_gmt(lap.get("startTimeGMT")),
            elapsed_s=num(lap.get("elapsedDuration")) or num(lap.get("duration")),
            moving_s=num(lap.get("movingDuration")) or num(lap.get("duration")),
            distance_m=num(lap.get("distance")),
            avg_hr=num(lap.get("averageHR")),
            max_hr=num(lap.get("maxHR")),
            avg_power=num(lap.get("averagePower")),
            avg_speed=num(lap.get("averageSpeed")),
            extra={
                k: v
                for k, v in lap.items()
                if k
                not in {
                    "lapIndex",
                    "startTimeGMT",
                    "elapsedDuration",
                    "duration",
                    "movingDuration",
                    "distance",
                    "averageHR",
                    "maxHR",
                    "averagePower",
                    "averageSpeed",
                }
                and v is not None
            },
        )
    for kind in ("hr", "power"):
        for zone in _as_list(data.get(f"{kind}_zones")):
            number, seconds = zone.get("zoneNumber"), num(zone.get("secsInZone"))
            if number is not None and seconds is not None:
                yield ActivityZoneTime(
                    activity_id, kind, int(number), seconds, num(zone.get("zoneLowBoundary"))
                )


# Helpers ----------------------------------------------------------------------------------


def _day(raw: RawRecord) -> date:
    """The calendar day of a daily item (its key is the date)."""
    return date.fromisoformat(raw.item_key[:10])


def num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _from_ms(value: Any) -> datetime | None:
    ms = num(value)
    return datetime.fromtimestamp(ms / 1000, UTC) if ms else None


def _local(value: Any) -> datetime | None:
    """A naive local timestamp: '2026-09-20 08:00:00' or '2026-09-20T08:00:00.0'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=None)
    except ValueError:
        return None


def _gmt(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _from_ms(value)
    parsed = _local(value)
    return parsed.replace(tzinfo=UTC) if parsed else None
