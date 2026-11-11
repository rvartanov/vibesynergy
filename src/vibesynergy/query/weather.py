"""Weather forecast for planning outdoor sessions (intervals.icu supporter forecast).

Read live from intervals.icu, not stored: a forecast is only useful while it is fresh.
Flags are plain thresholds for the coach; the coach decides outdoor or indoor.
"""

from __future__ import annotations

from typing import Any

from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

RAIN_MM = 2.0  # a wet ride or run from about this much rain in a day
GUST_MS = 12.0  # gusts that make an outdoor ride unpleasant or unsafe
COLD_C = 2.0  # risk of ice on the road at or below this minimum


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _day(raw: dict[str, Any]) -> dict[str, Any]:
    temp = raw.get("temp") if isinstance(raw.get("temp"), dict) else {}
    weather = raw.get("weather") or [{}]
    rain = _num(raw.get("rain")) or 0.0
    snow = _num(raw.get("snow")) or 0.0
    gust = _num(raw.get("wind_gust"))
    low = _num(temp.get("min"))
    flags = []
    if rain >= RAIN_MM:
        flags.append("rain")
    if snow > 0:
        flags.append("snow")
    if gust is not None and gust >= GUST_MS:
        flags.append("gusts")
    if low is not None and low <= COLD_C:
        flags.append("cold")
    return {
        "date": raw.get("id"),
        "temp_min_c": low,
        "temp_max_c": _num(temp.get("max")),
        "temp_day_c": _num(temp.get("day")),
        "wind_ms": _num(raw.get("wind_speed")),
        "gust_ms": gust,
        "rain_mm": rain,
        "snow_mm": snow,
        "summary": (weather[0] or {}).get("description"),
        "flags": flags,
    }


def forecast(client: IntervalsIcuClient, days: int = 9) -> dict[str, Any]:
    """Daily forecast for the first configured location, at most ``days`` days."""
    days = max(1, min(int(days), 14))
    payload = client.weather_forecast()
    places = [f for f in payload.get("forecasts") or [] if isinstance(f, dict)]
    if not places:
        return {"location": None, "days": [], "note": "No weather location set in intervals.icu."}
    place = places[0]
    daily = [d for d in place.get("daily") or [] if isinstance(d, dict)][:days]
    return {
        "location": place.get("label") or place.get("location"),
        "provider": place.get("provider"),
        "error": place.get("error"),
        "thresholds": {"rain_mm": RAIN_MM, "gust_ms": GUST_MS, "cold_c": COLD_C},
        "days": [_day(d) for d in daily],
    }
