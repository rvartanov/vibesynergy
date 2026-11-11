"""Every Garmin Connect dataset VibeSynergy collects and how to fetch it.

``call`` receives the ``garminconnect.Garmin`` session and, depending on the kind:
DAILY ``(api, "YYYY-MM-DD")``, RANGE ``(api, start, end)`` for one calendar month,
SNAPSHOT ``(api)``. Entity datasets (activities and their files) are fetched in source.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from vibesynergy.sources.base import DatasetKind, DatasetSpec

DAILY, RANGE, ENTITY, SNAPSHOT = (
    DatasetKind.DAILY,
    DatasetKind.RANGE,
    DatasetKind.ENTITY,
    DatasetKind.SNAPSHOT,
)
# Garmin revises sleep, HRV and readiness after later watch syncs.
REFETCH_DAYS = 7
# Weekly totals and other range summaries move slowly. A plain 15-minute sync leaves them
# until they are this old; an explicit sync or backfill still fetches them.
TREND_REFRESH_MINUTES = 60
# Authentication and rate-limit errors stop the dataset. Anything else stays inside the
# item, so one endpoint cannot blank the rest of the sync.
_FATAL_ERRORS = frozenset({"GarminConnectAuthenticationError", "GarminConnectTooManyRequestsError"})


@dataclass(frozen=True)
class GarminDataset:
    spec: DatasetSpec
    call: Callable[..., Any] | None = None


def _daily(name: str, description: str, call, *, enabled: bool = True) -> GarminDataset:
    spec = DatasetSpec(
        name,
        DAILY,
        description,
        initial_days=7,
        refetch_days=REFETCH_DAYS,
        default_enabled=enabled,
    )
    return GarminDataset(spec, call)


def _range(
    name: str,
    description: str,
    call,
    *,
    keep_history: bool = False,
    lookahead_days: int = 0,
    min_age_minutes: int = 0,
) -> GarminDataset:
    spec = DatasetSpec(
        name,
        RANGE,
        description,
        initial_days=31,
        refetch_days=REFETCH_DAYS,
        keep_history=keep_history,
        lookahead_days=lookahead_days,
        min_age_minutes=min_age_minutes,
    )
    return GarminDataset(spec, call)


def _snapshot(
    name: str, description: str, call, *, keep_history: bool = False, enabled: bool = True
) -> GarminDataset:
    spec = DatasetSpec(
        name, SNAPSHOT, description, keep_history=keep_history, default_enabled=enabled
    )
    return GarminDataset(spec, call)


def _pages(
    fetch: Callable[[int, int], Any], size: int = 100, limit: int = 5000, first: int = 0
) -> list:
    """Collect a paginated list endpoint (start, limit) until a short page. ``first`` is
    the index of the first item: 0 for most endpoints, 1 for the challenge endpoints."""
    items: list = []
    for start in range(first, first + limit, size):
        page = fetch(start, size) or []
        if isinstance(page, dict):  # some endpoints wrap the list
            page = next((v for v in page.values() if isinstance(v, list)), [])
        items.extend(page)
        if len(page) < size:
            break
    return items


def _profile(api) -> dict:
    return {
        "user_profile": api.get_user_profile(),
        "settings": api.get_userprofile_settings(),
        "full_name": api.get_full_name(),
        "unit_system": api.get_unit_system(),
        "profile_id": api.profile_id,
        "display_name": api.display_name,
    }


def _devices(api) -> dict:
    devices = api.get_devices() or []
    settings = {}
    for device in devices if isinstance(devices, list) else []:
        device_id = device.get("deviceId") if isinstance(device, dict) else None
        if device_id:
            settings[str(device_id)] = api.get_device_settings(str(device_id))
    return {
        "devices": devices,
        "settings": settings,
        "primary_training_device": api.get_primary_training_device(),
        "last_used": api.get_device_last_used(),
        "alarms": api.get_device_alarms(),
    }


def _gear(api) -> dict:
    gear = api.get_gear(str(api.profile_id)) if api.profile_id else []
    stats = {}
    for item in gear if isinstance(gear, list) else []:
        uuid = item.get("uuid") if isinstance(item, dict) else None
        if uuid:
            stats[uuid] = api.get_gear_stats(uuid)
    return {"gear": gear, "stats": stats}


def _is_fatal(exc: Exception) -> bool:
    """Authentication and rate-limit errors stop the dataset; anything else is per call."""
    return type(exc).__name__ in _FATAL_ERRORS


def _call_or_error(call: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return call(), None
    except Exception as exc:
        if _is_fatal(exc):
            raise
        return None, f"{type(exc).__name__}: {exc}"


def _challenges(api) -> dict:
    """Badge-challenge lists are one-based, like the other challenge endpoints."""
    parts = {
        "adhoc": lambda: _pages(api.get_adhoc_challenges, first=1),
        "badge": lambda: _pages(api.get_badge_challenges, first=1),
        "virtual_in_progress": lambda: _pages(api.get_inprogress_virtual_challenges, first=1),
        "available_badge": lambda: _pages(api.get_available_badge_challenges, first=1),
        "non_completed_badge": lambda: _pages(api.get_non_completed_badge_challenges, first=1),
    }
    payload: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, call in parts.items():
        value, error = _call_or_error(call)
        if error:
            errors[key] = error
        else:
            payload[key] = value
    if errors:
        payload["errors"] = errors
    return payload


def _span_weeks(start: str, end: str) -> int:
    """Weeks to request so one weekly call covers ``start`` through ``end``.

    garminconnect's weekly helpers default to 52 weeks. A month only needs the weeks
    that overlap it, which is still a single request.
    """
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return max(1, days // 7 + 1)


def _weekly_steps(api, start: str, end: str) -> Any:
    return api.get_weekly_steps(end, _span_weeks(start, end))


def _weekly_stress(api, start: str, end: str) -> Any:
    return api.get_weekly_stress(end, _span_weeks(start, end))


def _device_ids(devices: Any) -> list[str]:
    ids: list[str] = []
    for device in devices if isinstance(devices, list) else []:
        if isinstance(device, dict) and device.get("deviceId") not in (None, ""):
            ids.append(str(device["deviceId"]))
    return ids


def _device_solar(api, start: str, end: str) -> dict:
    """One solar call per device for the month. A device without solar is recorded
    on that device and does not drop the others."""
    devices, error = _call_or_error(api.get_devices)
    solar: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if error:
        errors["devices"] = error
    else:
        for device_id in _device_ids(devices):
            value, device_error = _call_or_error(
                lambda device_id=device_id: api.get_device_solar_data(device_id, start, end)
            )
            if device_error:
                errors[device_id] = device_error
            else:
                solar[device_id] = value
    payload: dict[str, Any] = {"solar": solar}
    if errors:
        payload["errors"] = errors
    return payload


def _goals(api) -> dict:
    # Current Connect goal-service is one-based; zero can silently return an empty list.
    # garminconnect paginates internally, with a bounded-loop failure instead of truncation.
    return {
        **{status: api.get_goals(status, 1, 100) for status in ("active", "future", "past")},
        "_collection": {"start": 1, "pagination": "garminconnect_until_empty"},
    }


def _scheduled(api, start: str, end: str) -> Any:
    year, month = start[:4], start[5:7]
    return api.get_scheduled_workouts(int(year), int(month))


def _wellness_zip(api, day: str) -> bytes:
    # /download-service/files/wellness/<date>: the day's monitoring FIT files as a zip.
    return api.download_health_snapshot(day)


def _critical_swim_speed(api) -> dict | None:
    from vibesynergy.sources.garmin import swim_speed

    return swim_speed.latest(api)


DATASETS: list[GarminDataset] = [
    # Activities: summary + details per activity, and the original FIT file.
    GarminDataset(
        DatasetSpec(
            "activities",
            ENTITY,
            "Activities: summary, laps/splits, HR and power time in zone, weather, gear, "
            "exercise sets",
            initial_days=7,
            refetch_days=2,
        )
    ),
    GarminDataset(
        DatasetSpec(
            "activity_files",
            ENTITY,
            "Original activity FIT files (streams, developer fields, device zones)",
            initial_days=7,
            refetch_days=2,
        )
    ),
    _daily(
        "wellness_files", "Daily monitoring FIT files (all-day HR, stress, steps)", _wellness_zip
    ),
    # Daily wellness.
    _daily(
        "user_summary",
        "Daily summary: steps, calories, floors, RHR, stress, body battery",
        lambda api, d: api.get_user_summary(d),
    ),
    _daily("heart_rate", "All-day heart rate (2 min)", lambda api, d: api.get_heart_rates(d)),
    _daily("rhr", "Resting heart rate for one day", lambda api, d: api.get_rhr_day(d)),
    _daily(
        "sleep",
        "Sleep: score, stages, SpO2, respiration, HRV, restlessness",
        lambda api, d: api.get_sleep_data(d),
    ),
    _range(
        "sleep_summary",
        "Sleep summaries over a date range",
        lambda api, s, e: api.get_sleep_daily(s, e),
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _daily("hrv", "Overnight HRV: status, baseline, readings", lambda api, d: api.get_hrv_data(d)),
    _range(
        "hrv_range",
        "Heart-rate variability over a date range",
        lambda api, s, e: api.get_hrv_data_range(s, e),
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _daily(
        "stress", "All-day stress and body battery (3 min)", lambda api, d: api.get_stress_data(d)
    ),
    _daily(
        "body_battery_events", "Body Battery events", lambda api, d: api.get_body_battery_events(d)
    ),
    _daily(
        "respiration",
        "Respiration, intraday and summary",
        lambda api, d: api.get_respiration_data(d),
    ),
    _daily("spo2", "Pulse Ox, intraday and summary", lambda api, d: api.get_spo2_data(d)),
    _daily("steps", "Intraday steps (15 min)", lambda api, d: api.get_steps_data(d)),
    _daily("floors", "Intraday floors", lambda api, d: api.get_floors(d)),
    _daily(
        "intensity_minutes", "Intensity minutes", lambda api, d: api.get_intensity_minutes_data(d)
    ),
    _daily(
        "all_day_events",
        "Auto-detected events (naps, activities)",
        lambda api, d: api.get_all_day_events(d),
    ),
    _daily(
        "lifestyle_logging",
        "Lifestyle logging (alcohol, caffeine, …)",
        lambda api, d: api.get_lifestyle_logging_data(d),
    ),
    _daily("hydration", "Hydration", lambda api, d: api.get_hydration_data(d)),
    # Training.
    _daily(
        "training_readiness",
        "Training readiness including factors",
        lambda api, d: api.get_training_readiness(d),
    ),
    _daily(
        "morning_training_readiness",
        "Morning training readiness",
        lambda api, d: api.get_morning_training_readiness(d),
    ),
    _daily(
        "training_status",
        "Training status, load balance, acute/chronic load",
        lambda api, d: api.get_training_status(d),
    ),
    _daily("max_metrics", "VO2max (running and cycling)", lambda api, d: api.get_max_metrics(d)),
    _range(
        "max_metrics_range",
        "VO2max over a date range",
        lambda api, s, e: api.get_max_metrics_range(s, e),
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _daily("fitness_age", "Fitness age", lambda api, d: api.get_fitnessage_data(d)),
    _range("endurance_score", "Endurance score", lambda api, s, e: api.get_endurance_score(s, e)),
    _range("hill_score", "Hill score", lambda api, s, e: api.get_hill_score(s, e)),
    _range(
        "race_predictions",
        "Race predictions (5k, 10k, half, marathon)",
        lambda api, s, e: api.get_race_predictions(s, e, "daily"),
    ),
    _range(
        "running_tolerance", "Running tolerance", lambda api, s, e: api.get_running_tolerance(s, e)
    ),
    _range(
        "weekly_steps",
        "Weekly step totals",
        _weekly_steps,
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _range(
        "weekly_stress",
        "Weekly stress totals",
        _weekly_stress,
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _range(
        "weekly_intensity_minutes",
        "Weekly intensity minutes",
        lambda api, s, e: api.get_weekly_intensity_minutes(s, e),
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _range(
        "lactate_threshold",
        "Lactate threshold history",
        lambda api, s, e: api.get_lactate_threshold(latest=False, start_date=s, end_date=e),
    ),
    # Body.
    _range(
        "body_composition",
        "Body composition and weigh-ins",
        lambda api, s, e: api.get_body_composition(s, e),
    ),
    _range("weigh_ins", "Weigh-ins with their sources", lambda api, s, e: api.get_weigh_ins(s, e)),
    _range(
        "blood_pressure", "Blood pressure readings", lambda api, s, e: api.get_blood_pressure(s, e)
    ),
    _range(
        "body_battery", "Daily Body Battery reports", lambda api, s, e: api.get_body_battery(s, e)
    ),
    # Plans and context.
    # The calendar looks ahead. GarminSource applies [calendar] lookahead_months (12 months,
    # one year, when unset). Every version stays mapped, because Garmin drops past plan days
    # from its replies and planned vs done needs them.
    _range(
        "scheduled_workouts",
        "Calendar: scheduled workouts, plan days and events",
        _scheduled,
        keep_history=True,
    ),
    _snapshot("profile", "Profile and user settings", _profile),
    _snapshot(
        "heart_rate_zones",
        "Heart-rate zones (history of changes)",
        lambda api: api.get_heart_rate_zones(),
        keep_history=True,
    ),
    _snapshot(
        "power_zones",
        "Power zones (history of changes)",
        lambda api: api.get_power_zones(),
        keep_history=True,
    ),
    _snapshot(
        "cycling_ftp",
        "Cycling FTP (history of changes)",
        lambda api: api.get_cycling_ftp(),
        keep_history=True,
    ),
    _snapshot(
        "critical_swim_speed",
        "Critical swim speed (history of changes)",
        lambda api: _critical_swim_speed(api),
        keep_history=True,
    ),
    _snapshot(
        "lactate_threshold_latest",
        "Latest lactate threshold (history of changes)",
        lambda api: api.get_lactate_threshold(),
        keep_history=True,
    ),
    _snapshot("devices", "Devices, their settings and last use", _devices),
    _range(
        "device_solar",
        "Solar charging for each device",
        _device_solar,
        min_age_minutes=TREND_REFRESH_MINUTES,
    ),
    _snapshot("gear", "Gear and usage statistics", _gear),
    _snapshot("personal_records", "Personal records", lambda api: api.get_personal_record()),
    _snapshot("badges", "Earned badges", lambda api: api.get_earned_badges()),
    _snapshot("challenges", "Challenges", _challenges),
    _snapshot("goals", "Goals", _goals),
    _snapshot("workouts", "Workout library", lambda api: _pages(api.get_workouts)),
    _snapshot("training_plans", "Training plans", lambda api: api.get_training_plans()),
    _snapshot("activity_types", "Activity type catalogue", lambda api: api.get_activity_types()),
    # garminconnect 0.3.16 has no method for these, so they are not collected: power-duration
    # curve, acclimation, energy balance, custom foods, activity FIT messages, courses
    # (details and GPX), Garmin Coach workouts, training-plan workouts, respiration trend,
    # stats range, HRV trend, VO2max trend, training-load trend. The endpoints that do
    # exist are the datasets above (`sleep_summary`, `hrv_range`, `max_metrics_range`,
    # the weekly totals, `device_solar`) plus daily `rhr`. Running-tolerance's weekly
    # aggregate is already `running_tolerance`. Activity FIT messages would repeat
    # `activity_files`. Nutrition settings belong with the disabled nutrition datasets
    # below and are left out.
    # Off by default: enable with extra_datasets = [...] under [sources.garmin].
    _daily(
        "nutrition_food_log",
        "Nutrition: food log",
        lambda api, d: api.get_nutrition_daily_food_log(d),
        enabled=False,
    ),
    _daily(
        "nutrition_meals",
        "Nutrition: meals",
        lambda api, d: api.get_nutrition_daily_meals(d),
        enabled=False,
    ),
    _daily(
        "menstrual",
        "Menstrual cycle day data",
        lambda api, d: api.get_menstrual_data_for_date(d),
        enabled=False,
    ),
    _snapshot(
        "pregnancy", "Pregnancy summary", lambda api: api.get_pregnancy_summary(), enabled=False
    ),
]

BY_NAME = {d.spec.name: d for d in DATASETS}
