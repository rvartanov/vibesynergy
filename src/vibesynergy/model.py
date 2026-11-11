"""Canonical records that source mappers produce.

Mappers yield these; the normaliser writes each type to its table (or Parquet dataset) with
the source and the raw item it came from. Times are timezone-aware UTC; ``local_date`` is the
calendar day in the athlete's time zone; quantities use SI units (m, s, kg, W, bpm).
Fields a table has no column for go in ``extra`` so nothing a source reported is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class DailyMetric:
    """One value per day for a catalogue metric, e.g. ``hr.resting`` = 48 bpm."""

    date: date
    metric: str
    value: float
    unit: str
    subject_id: int = 1


@dataclass(frozen=True)
class Activity:
    source_id: str  # the source's own id (Garmin activityId, FIT file id, …)
    start_utc: datetime
    local_date: date
    sport: str
    tz_offset_s: int | None = None
    sub_sport: str | None = None
    name: str | None = None
    elapsed_s: float | None = None
    moving_s: float | None = None
    distance_m: float | None = None
    ascent_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power: float | None = None
    norm_power: float | None = None
    avg_speed: float | None = None
    calories: float | None = None
    training_load: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    device: str | None = None
    fit_id: str | None = None  # links the activity to its original FIT file and streams
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class ActivityLap:
    activity_source_id: str
    lap_index: int
    start_utc: datetime | None = None
    elapsed_s: float | None = None
    moving_s: float | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power: float | None = None
    avg_speed: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivityZoneTime:
    activity_source_id: str
    kind: str  # "hr" | "power" | "pace"
    zone: int
    seconds: float
    low: float | None = None  # lower boundary of the zone (bpm, W or m/s for pace)


@dataclass(frozen=True)
class SleepSession:
    source_id: str
    local_date: date  # the morning the night ends on
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    duration_s: float | None = None
    deep_s: float | None = None
    light_s: float | None = None
    rem_s: float | None = None
    awake_s: float | None = None
    score: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class SleepStage:
    session_source_id: str
    start_utc: datetime
    end_utc: datetime
    stage: str  # deep | light | rem | awake


@dataclass(frozen=True)
class BodyMeasurement:
    """A single timed measurement (weigh-in, blood pressure reading, …)."""

    source_id: str
    measured_at: datetime
    metric: str  # catalogue id: body.weight, bp.systolic, …
    value: float
    unit: str
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class ZoneSet:
    """Training zones valid from a date on; history is kept so old activities use old zones."""

    kind: str  # "hr" | "power" | "pace" (pace boundaries are speed in m/s, source-only)
    sport: str  # "default", "running", "cycling", …
    valid_from: date
    boundaries: tuple[float, ...]  # ascending; see extra["boundary"] for low vs. high
    threshold: float | None = None  # LTHR (bpm) or FTP (W)
    max_value: float | None = None  # max HR
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class PlannedItem:
    """An entry on the training calendar: a coaching-plan workout, a workout you scheduled,
    or an event such as a race. Past entries stay, so planned can be compared with done."""

    source_id: str
    date: date
    kind: str  # plan_workout | workout | event | commitment | mirror
    title: str | None = None
    sport: str | None = None
    plan_id: str | None = None  # the training plan it belongs to
    is_race: bool = False
    primary: bool = False  # the event a plan builds towards
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class CalendarSpan:
    """The dates one calendar reply is complete for. Planned items on these dates that an
    earlier reply listed and this one no longer does are gone (a coach dropped or swapped the
    session); earlier dates keep what was planned. Not stored itself."""

    start: date
    end: date
    include_overlapping: bool = False  # calendar commitments can span the window start


@dataclass(frozen=True)
class TrainingPlan:
    """A coaching plan (e.g. Garmin Run Coach towards a race)."""

    source_id: str
    name: str
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None  # Scheduled | Completed | …
    plan_type: str | None = None  # e.g. GarminRunningCoachEventBased
    level: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    subject_id: int = 1


@dataclass(frozen=True)
class FitFile:
    """An original FIT file as decoded; ``fit_id`` identifies it across copies."""

    fit_id: str
    file_type: str  # activity | monitoring_a | monitoring_b | …
    time_created: datetime | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial_number: int | None = None
    start_utc: datetime | None = None
    sport: str | None = None
    source_activity_id: str | None = None  # e.g. the Garmin activityId it was downloaded for
    messages: dict[str, int] = field(default_factory=dict)  # message counts, for inspection


@dataclass(frozen=True)
class IcuEvent:
    """One intervals.icu calendar event cached locally.

    ``id`` is the intervals.icu event id. ``source`` is ``plan`` when the event has an
    ``external_id``, and ``other`` when it does not (the athlete's own event, or one
    intervals.icu created). ``session_key`` groups a brick: ``:fiets`` and ``:loop`` on
    the same date share that date. Other plan events keep ``external_id`` as the session
    key (``<date>:<sport>``).
    """

    id: str
    category: str
    date: date
    source: str
    fetched_at: datetime
    external_id: str | None = None
    session_key: str | None = None
    brick_part: str | None = None  # "fiets" (bike) or "loop" (run)
    type: str | None = None
    name: str | None = None
    description: str | None = None
    workout_doc: Any = None
    tags: list[str] = field(default_factory=list)
    for_week: bool = False
    targets: dict[str, Any] = field(default_factory=dict)
    updated: datetime | None = None  # when intervals.icu last changed the event
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Stream:
    """Per-second samples of one activity, written to Parquet (one file per FIT id)."""

    fit_id: str
    columns: dict[str, list[Any]]  # "t" (UTC datetimes) plus one list per channel


@dataclass(frozen=True)
class WellnessSamples:
    """Intraday samples (all-day HR, stress, …) written to Parquet, one file per key."""

    key: str
    ts: list[datetime]
    metric: list[str]
    value: list[float]


Record = (
    DailyMetric
    | Activity
    | ActivityLap
    | ActivityZoneTime
    | SleepSession
    | SleepStage
    | BodyMeasurement
    | ZoneSet
    | PlannedItem
    | CalendarSpan
    | TrainingPlan
    | FitFile
    | Stream
    | WellnessSamples
    | IcuEvent
)
