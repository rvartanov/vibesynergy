"""Critical swim speed (CSS) in Garmin Connect: read the latest value and save a new one.

Garmin keeps CSS as a history of measurements in millimetres per second (1000 mm/s is
1:40 /100 m). Saving adds a manual measurement, the way Garmin Connect's web app does; the
caller reads the latest value back, because Garmin may keep another one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_CSS_URL = "/biometric-service/criticalSwimSpeed"
# What Garmin accepts; outside this it answers 400 (bpauli/gccli, Nov 2026).
MIN_MM_PER_S = 333
MAX_MM_PER_S = 2000


def mm_per_s(seconds_per_100m: float) -> int:
    """VibeSynergy's CSS (seconds per 100 m) as Garmin's mm/s."""
    return round(100_000 / float(seconds_per_100m))


def seconds_per_100m(mm_per_second: float) -> float:
    return round(100_000 / float(mm_per_second), 1)


def latest(api: Any, day: date | None = None) -> dict[str, Any] | None:
    """Garmin's latest CSS measurement on or before ``day`` (default: tomorrow, so a value
    saved today in any time zone is included); None when there is none."""
    day = day or date.today() + timedelta(days=1)
    reply = api.connectapi(f"{_CSS_URL}/latest/{day.isoformat()}")
    if not isinstance(reply, dict) or not reply.get("criticalSwimSpeed"):
        return None
    return reply


def body(mm_per_second: int, now: datetime, timezone: str) -> dict[str, Any]:
    """A manual CSS measurement at ``now`` (aware), as Garmin Connect's web app sends it."""
    if not MIN_MM_PER_S <= mm_per_second <= MAX_MM_PER_S:
        raise ValueError(
            f"critical swim speed {mm_per_second} mm/s is outside the "
            f"{MIN_MM_PER_S} to {MAX_MM_PER_S} mm/s Garmin accepts"
        )
    return {
        "version": 0,
        "criticalSwimSpeed": int(mm_per_second),
        "measurementTimestampLocal": _stamp(now.astimezone(ZoneInfo(timezone))),
        "measurementTimestampGMT": _stamp(now.astimezone(ZoneInfo("UTC"))),
        "biometricSourceType": "MANUAL",
    }


def save(api: Any, mm_per_second: int, *, now: datetime, timezone: str) -> dict[str, Any] | None:
    """Save a manual CSS in Garmin Connect; returns Garmin's echo (with its version)."""
    payload = body(mm_per_second, now, timezone)
    log.info("Writing Garmin critical swim speed (%s mm/s)", mm_per_second)
    response = api.client.post("connectapi", _CSS_URL, json=payload)
    try:
        return response.json()
    except Exception:  # Garmin may answer without a body
        return None


def _stamp(moment: datetime) -> str:
    # Milliseconds with at least two digits, like the web app: 7 ms is ".07".
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:02d}"
