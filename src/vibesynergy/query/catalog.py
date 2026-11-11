"""The metric catalogue (docs/MCP.md §2.1).

Known metrics are described here; any other metric found in the data is added
automatically with its unit, so new sources show up without code changes.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass

from sqlalchemy import Engine, func, select

from vibesynergy.store.schema import daily_unified


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    unit: str
    domain: str
    daily_agg: str = "last"  # how several values of one day combine (for info)
    period_agg: str = "mean"  # default aggregation over weeks/months
    higher_is_better: bool | None = None
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _m(
    id: str,
    label: str,
    unit: str,
    agg: str = "mean",
    better: bool | None = None,
    description: str = "",
) -> Metric:
    return Metric(
        id,
        label,
        unit,
        id.split(".", 1)[0],
        period_agg=agg,
        higher_is_better=better,
        description=description,
    )


KNOWN: dict[str, Metric] = {
    m.id: m
    for m in [
        _m("daily.steps", "Steps", "count", "sum", True),
        _m("daily.distance", "Distance walked/run all day", "m", "sum", True),
        _m("daily.floors", "Floors climbed", "count", "sum", True),
        _m("daily.active_calories", "Active calories", "kcal", "sum"),
        _m("daily.total_calories", "Total calories burned", "kcal", "sum"),
        _m("daily.intensity_minutes.moderate", "Moderate intensity minutes", "min", "sum", True),
        _m("daily.intensity_minutes.vigorous", "Vigorous intensity minutes", "min", "sum", True),
        _m("daily.elevation", "Elevation climbed all day", "m", "sum", True),
        _m("hr.resting", "Resting heart rate", "bpm", "mean", False),
        _m("hr.min", "Lowest heart rate of the day", "bpm"),
        _m("hr.max", "Highest heart rate of the day", "bpm"),
        _m("hr.avg", "Average heart rate of the day", "bpm"),
        _m("hrv.night_avg", "Overnight HRV (average)", "ms", "mean", True),
        _m("hrv.weekly_avg", "HRV 7-day average", "ms", "mean", True),
        _m("hrv.baseline_low", "HRV baseline, lower bound", "ms"),
        _m("hrv.baseline_high", "HRV baseline, upper bound", "ms"),
        _m("hrv.night_5min_high", "Overnight HRV, highest 5 minutes", "ms"),
        _m("hr.spot", "Heart rate, single measurement", "bpm"),
        _m(
            "sleep.score", "Sleep score", "score", "mean", True, "The night that ends on this date."
        ),
        _m("sleep.duration", "Time asleep", "s", "mean", True),
        _m("sleep.deep", "Deep sleep", "s"),
        _m("sleep.light", "Light sleep", "s"),
        _m("sleep.rem", "REM sleep", "s"),
        _m("sleep.awake", "Awake during the night", "s", "mean", False),
        _m("sleep.avg_hr", "Average heart rate asleep", "bpm", "mean", False),
        _m("sleep.avg_hrv", "Average HRV asleep", "ms", "mean", True),
        _m("sleep.avg_respiration", "Breathing rate asleep", "brpm"),
        _m("sleep.spo2_avg", "SpO2 asleep", "pct"),
        _m("sleep.resting_hr", "Resting heart rate (sleep record)", "bpm", "mean", False),
        _m("sleep.avg_stress", "Average stress asleep", "score", "mean", False),
        _m("sleep.body_battery_change", "Body Battery charged overnight", "score", "mean", True),
        _m("sleep.restless_moments", "Restless moments", "count", "mean", False),
        _m("sleep.skin_temp_deviation", "Skin temperature change overnight", "degC"),
        _m("spo2.avg", "Blood oxygen (SpO2), average", "pct"),
        _m("spo2.min", "Blood oxygen (SpO2), lowest", "pct"),
        _m("resp.waking_avg", "Breathing rate awake", "brpm"),
        _m("recovery.readiness", "Training readiness", "score", "mean", True),
        _m("recovery.stress.avg", "Average stress", "score", "mean", False),
        _m("recovery.body_battery.max", "Body Battery high", "score", "mean", True),
        _m("recovery.body_battery.min", "Body Battery low", "score", "mean", True),
        _m("recovery.body_battery.charged", "Body Battery charged", "score", "mean", True),
        _m("recovery.body_battery.drained", "Body Battery drained", "score"),
        _m("recovery.stress.high_minutes", "High-stress time", "min", "mean", False),
        _m("recovery.stress.rest_minutes", "Rest time (low stress)", "min", "mean", True),
        _m("body.weight", "Weight", "kg", "mean", None),
        _m("body.fat_pct", "Body fat", "pct", "mean", None),
        _m("body.muscle_mass", "Muscle mass", "kg"),
        _m("body.fat_mass", "Fat mass", "kg"),
        _m("body.lean_mass", "Lean mass", "kg"),
        _m("body.water_mass", "Water mass", "kg"),
        _m("body.bone_mass", "Bone mass", "kg"),
        _m("body.height", "Height", "m"),
        _m("body.temperature", "Body temperature", "degC"),
        _m("bp.systolic", "Blood pressure, systolic", "mmHg", "mean", False),
        _m("bp.diastolic", "Blood pressure, diastolic", "mmHg", "mean", False),
        _m("bp.pulse", "Pulse (blood pressure measurement)", "bpm"),
        _m("fitness.vo2max.run", "VO2max (running)", "ml_kg_min", "mean", True),
        _m("fitness.vo2max.ride", "VO2max (cycling)", "ml_kg_min", "mean", True),
        _m("fitness.fitness_age", "Fitness age", "years", "mean", False),
        _m("fitness.race_pred.5k", "Race prediction, 5 km", "s", "mean", False),
        _m("fitness.race_pred.10k", "Race prediction, 10 km", "s", "mean", False),
        _m("fitness.race_pred.half", "Race prediction, half marathon", "s", "mean", False),
        _m("fitness.race_pred.marathon", "Race prediction, marathon", "s", "mean", False),
        _m("activity.count", "Activities", "count", "sum", None, "From unified activities."),
        _m("activity.duration", "Training time", "s", "sum", None, "From unified activities."),
        _m("activity.distance", "Training distance", "m", "sum", None, "From unified activities."),
        _m("activity.ascent", "Training climbing", "m", "sum", None, "From unified activities."),
        _m(
            "activity.load",
            "Training load (Garmin)",
            "load",
            "sum",
            None,
            "Sum of Garmin's per-activity training load.",
        ),
        _m(
            "checkin.sleep_quality",
            "Check-in: sleep quality",
            "score_1_5",
            "mean",
            True,
            "Your morning check-in in the diary: 1 = very poor … 5 = very good.",
        ),
        _m(
            "checkin.soreness",
            "Check-in: muscle soreness",
            "score_1_5",
            "mean",
            False,
            "Your morning check-in in the diary: 1 = none … 5 = very sore.",
        ),
        _m(
            "checkin.mood",
            "Check-in: mood",
            "score_1_5",
            "mean",
            True,
            "Your morning check-in in the diary: 1 = very low … 5 = very good.",
        ),
        _m(
            "checkin.stress",
            "Check-in: stress",
            "score_1_5",
            "mean",
            False,
            "Your morning check-in in the diary: 1 = none … 5 = very high.",
        ),
        _m(
            "checkin.motivation",
            "Check-in: motivation",
            "score_1_5",
            "mean",
            True,
            "Your morning check-in in the diary: 1 = none … 5 = very high.",
        ),
        _m(
            "pain.max",
            "Pain, worst of the day",
            "score_0_10",
            "max",
            False,
            "The highest pain logged in the diary's injury log that day, over all injuries.",
        ),
    ]
}

# Metrics computed from unified activities rather than stored daily values.
ACTIVITY_METRICS = {
    "activity.count": ("count", None),
    "activity.duration": ("sum", "moving_s"),
    "activity.distance": ("sum", "distance_m"),
    "activity.ascent": ("sum", "ascent_m"),
    "activity.load": ("sum", "training_load"),
}

# Metrics read from the diary (vibesynergy.query.journal): table and column.
DIARY_METRICS = {
    "checkin.sleep_quality": ("checkin", "sleep_quality"),
    "checkin.soreness": ("checkin", "soreness"),
    "checkin.mood": ("checkin", "mood"),
    "checkin.stress": ("checkin", "stress"),
    "checkin.motivation": ("checkin", "motivation"),
    "pain.max": ("injury_log", "pain"),
}

# Everything not stored in daily_unified.
COMPUTED = set(ACTIVITY_METRICS) | set(DIARY_METRICS)

# Metric ids that live in daily_unified. The table stores one ``value`` per metric id,
# not one column per metric. Activity totals and diary scores are computed elsewhere.
DAILY_UNIFIED_METRICS = frozenset(KNOWN) - COMPUTED


def catalogue(engine: Engine, *, coverage: bool = True) -> dict[str, dict]:
    """Known metrics plus every metric present in the data, with coverage."""
    entries = {mid: m.to_dict() for mid, m in KNOWN.items()}
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                daily_unified.c.metric,
                func.min(daily_unified.c.unit),
                func.min(daily_unified.c.date),
                func.max(daily_unified.c.date),
                func.count(),
                func.group_concat(daily_unified.c.source.distinct()),
            ).group_by(daily_unified.c.metric)
        ).all()
    for metric, unit, first, last, days, sources in rows:
        entry = entries.setdefault(
            metric,
            Metric(metric, metric, unit, metric.split(".", 1)[0]).to_dict(),
        )
        if coverage:
            span = (last - first).days + 1
            entry["coverage"] = {
                "first": str(first),
                "last": str(last),
                "days_with_data": days,
                "pct_of_days": round(100 * days / span, 1),
                "sources": sorted((sources or "").split(",")),
            }
    if coverage:
        _diary_coverage(engine, entries)
    return dict(sorted(entries.items()))


def _diary_coverage(engine: Engine, entries: dict[str, dict]) -> None:
    from vibesynergy.store.schema import checkin, injury_log

    tables = {"checkin": checkin, "injury_log": injury_log}
    with engine.connect() as conn:
        for metric, (table_name, column) in DIARY_METRICS.items():
            table = tables[table_name]
            col = table.c[column]
            first, last, days = conn.execute(
                select(
                    func.min(table.c.date),
                    func.max(table.c.date),
                    func.count(table.c.date.distinct()),
                ).where(col.is_not(None))
            ).one()
            if first is None:
                continue
            span = (last - first).days + 1
            entries[metric]["coverage"] = {
                "first": str(first),
                "last": str(last),
                "days_with_data": days,
                "pct_of_days": round(100 * days / span, 1),
                "sources": ["diary"],
            }


def check_metrics(engine: Engine, metrics: list[str]) -> None:
    """Raise a helpful error for unknown metric ids."""
    known = set(catalogue(engine, coverage=False)) | COMPUTED
    for metric in metrics:
        if metric not in known:
            close = difflib.get_close_matches(metric, sorted(known), n=5, cutoff=0.4)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise ValueError(f"unknown metric {metric!r}{hint} (see vibesynergy_catalog)")
