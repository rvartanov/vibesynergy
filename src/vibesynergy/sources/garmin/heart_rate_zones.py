"""Write heart-rate and power zone settings to Garmin Connect, only on the athlete's request.

Garmin keeps one heart-rate profile per sport (DEFAULT, RUNNING, CYCLING, SWIMMING) with
five zone floors, max HR and LTHR, and one power profile per sport with FTP and seven zone
floors. A write sends the whole list back with one profile changed, then the caller reads
it back: Garmin answers 200 even when it keeps its own value.
"""

from __future__ import annotations

import logging
from typing import Any

from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.garmin.client import connect

log = logging.getLogger(__name__)

_HR_ZONES_URL = "/biometric-service/heartRateZones"
# Garmin Connect's own web app saves one sport per call here; PUT of every sport to
# /powerZones answers 500 (1 Nov 2026).
_POWER_ZONES_URL = "/biometric-service/powerZones/all"
_POWER_KEYS = ("sport", "functionalThresholdPower", *(f"zone{n}Floor" for n in range(1, 8)))


def set_heart_rate_zones(
    secrets: SecretStore | None = None,
    *,
    max_hr: float | None = None,
    lthr: float | None = None,
    floors: list[int] | None = None,
    sport: str = "DEFAULT",
    api: Any = None,
) -> dict[str, Any] | None:
    """Update max HR, LTHR and (optionally) zone floors 2-5 of one Garmin HR profile."""
    if max_hr is None and lthr is None and not floors:
        return None
    api = api or connect(secrets)
    zones = list(api.get_heart_rate_zones())
    if not zones:
        log.info("Garmin HR zone write skipped: no zone profiles returned")
        return None
    sport_key = sport.upper()
    touched = False
    updated: list[dict[str, Any]] = []
    for entry in zones:
        row = dict(entry)
        if str(row.get("sport", "DEFAULT")).upper() != sport_key:
            updated.append(row)
            continue
        if max_hr is not None:
            row["maxHeartRateUsed"] = round(max_hr)
        if lthr is not None:
            row["lactateThresholdHeartRateUsed"] = round(lthr)
        if floors and len(floors) == 4:
            for number, value in zip((2, 3, 4, 5), floors, strict=True):
                row[f"zone{number}Floor"] = int(value)
            if row.get("zone1Floor") is not None and row["zone1Floor"] >= floors[0]:
                row["zone1Floor"] = max(floors[0] - 20, 0)
        row["changeState"] = "CHANGED"
        touched = True
        updated.append(row)
    if not touched:
        log.info("Garmin HR zone write skipped: sport profile %s not found", sport)
        return None
    log.info("Writing Garmin HR zones for %s (max_hr=%s, lthr=%s)", sport_key, max_hr, lthr)
    response = api.client.put("connectapi", _HR_ZONES_URL, json=updated)
    return _json(response)


def hr_profile(api: Any, sport: str) -> dict[str, Any] | None:
    for entry in api.get_heart_rate_zones() or []:
        if str(entry.get("sport", "DEFAULT")).upper() == sport.upper():
            return dict(entry)
    return None


def set_power_zones(
    api: Any,
    *,
    sport: str,
    ftp: float,
    floors: list[int] | None = None,
) -> dict[str, Any] | None:
    """Update FTP (and the seven floors) of one Garmin power profile.

    Without ``floors`` the existing floors are scaled to the new FTP.
    """
    zones = list(api.get_power_zones() or [])
    sport_key = sport.upper()
    updated: list[dict[str, Any]] = []
    touched = False
    for entry in zones:
        row = dict(entry)
        if str(row.get("sport", "")).upper() != sport_key:
            continue
        old = float(row.get("functionalThresholdPower") or 0)
        row["functionalThresholdPower"] = round(ftp)
        if floors:
            for number, value in enumerate(floors, start=1):
                row[f"zone{number}Floor"] = int(value)
        elif old > 0:
            for number in range(1, 8):
                key = f"zone{number}Floor"
                if row.get(key):
                    row[key] = round(float(row[key]) * ftp / old)
        touched = True
        updated.append({key: row[key] for key in _POWER_KEYS if key in row})
    if not touched:
        log.info("Garmin power zone write skipped: sport profile %s not found", sport)
        return None
    log.info("Writing Garmin power zones for %s (ftp=%s)", sport_key, ftp)
    response = api.client.put("connectapi", _POWER_ZONES_URL, json=updated)
    return _json(response)


def power_profile(api: Any, sport: str) -> dict[str, Any] | None:
    for entry in api.get_power_zones() or []:
        if str(entry.get("sport", "")).upper() == sport.upper():
            return dict(entry)
    return None


def _json(response: Any) -> dict[str, Any] | None:
    try:
        return response.json()
    except Exception:  # Garmin may answer 204 without a body
        return None


_HR_KEYS = (
    "trainingMethod",
    "lactateThresholdHeartRateUsed",
    "maxHeartRateUsed",
    *(f"zone{n}Floor" for n in range(1, 6)),
)
_USER_SETTINGS_URL = "/userprofile-service/userprofile/user-settings"


def copy_profile(api: Any, source: str = "RUNNING", target: str = "DEFAULT") -> dict | None:
    """Make one HR profile equal to another (DEFAULT is always RUNNING)."""
    zones = list(api.get_heart_rate_zones())
    src = next((z for z in zones if str(z.get("sport", "")).upper() == source), None)
    if src is None:
        return None
    updated = []
    for entry in zones:
        row = dict(entry)
        if str(row.get("sport", "DEFAULT")).upper() == target:
            for key in _HR_KEYS:
                row[key] = src.get(key)
            row["changeState"] = "CHANGED"
        updated.append(row)
    return _json(api.client.put("connectapi", _HR_ZONES_URL, json=updated))


def set_lactate_threshold(
    api: Any, *, lthr: float | None = None, pace_s_per_km: float | None = None
) -> dict[str, Any]:
    """The profile's lactate threshold (Performance stats), and auto-detection off.

    Garmin keeps the threshold speed as seconds per metre (5:10/km = 0.31)."""
    data: dict[str, Any] = {"thresholdHeartRateAutoDetected": False, "ftpAutoDetected": False}
    if lthr is not None:
        data["lactateThresholdHeartRate"] = round(lthr)
    if pace_s_per_km is not None:
        data["lactateThresholdSpeed"] = round(pace_s_per_km / 1000, 5)
    api.client.put("connectapi", _USER_SETTINGS_URL, json={"userData": data})
    return lactate_threshold(api)


def lactate_threshold(api: Any) -> dict[str, Any]:
    data = (api.client.connectapi(_USER_SETTINGS_URL) or {}).get("userData") or {}
    speed = data.get("lactateThresholdSpeed")
    return {
        "lthr": data.get("lactateThresholdHeartRate"),
        "pace_s_per_km": round(speed * 1000, 1) if isinstance(speed, (int, float)) else None,
        "auto_detect": bool(data.get("thresholdHeartRateAutoDetected"))
        or bool(data.get("ftpAutoDetected")),
    }
