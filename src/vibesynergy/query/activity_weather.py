"""Weather in the training calendar, from sources independent of intervals.icu.

- A done outdoor session: Garmin's own activity weather, else Open-Meteo at its start point
  and hour (``publication_plugins._weather``). Kept once found, since it no longer changes.
- Coming days: Open-Meteo's daily forecast for the place chosen in Settings → System → Weather
  on the calendar, else at the start of the last outdoor session; two decimals (about a
  kilometre) sent, cached for an hour per place. ``places`` looks a place up by name
  (Open-Meteo geocoding; only the typed name leaves).
Indoor and virtual sessions get none. Open-Meteo needs no key; only coordinates leave.
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any

import requests
from sqlalchemy import select

from vibesynergy.query import publication_format, publication_plugins, views
from vibesynergy.store.schema import activity, activity_unified

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
PLUGIN = "calendar_weather"
CACHE_VERSION = "1"
FORECAST_SECONDS = 3600
_FORECAST: dict[str, Any] = {"at": 0.0, "value": None, "point": None}


def for_day(rt, day: date, language: str = "nl", *, http=requests.get) -> list[dict[str, Any]]:
    """The weather of each outdoor session on ``day``: id, line and source."""
    with rt.engine.connect() as conn:
        ids = conn.execute(
            select(activity_unified.c.group_id)
            .where(activity_unified.c.local_date == day)
            .order_by(activity_unified.c.start_utc)
        ).scalars()
        ids = list(ids)
    out = []
    for group_id in ids:
        found = for_activity(rt, group_id, language, http=http)
        if found:
            out.append({"id": group_id, **found})
    return out


_PLACE = re.compile(r"[^\W\d_][\w .,'()/-]{0,59}")


def place(rt, group_id: str) -> str | None:
    """Where the session was: Garmin's place name for it (e.g. 'Leeuwarden'), if it has one."""
    with rt.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group_id)
        )
        for ref in sources or []:
            source, _, source_id = str(ref).partition(":")
            if source != "garmin":
                continue
            extra = conn.scalar(
                select(activity.c.extra).where(
                    activity.c.source == source, activity.c.source_id == source_id
                )
            )
            name = (extra or {}).get("locationName") if isinstance(extra, dict) else None
            if isinstance(name, str) and _PLACE.fullmatch(name.strip()):
                return name.strip()
    return None


def for_activity(rt, activity_id: str, language: str = "nl", *, http=requests.get):
    group = views.resolve_activity(rt.engine, activity_id)
    if publication_plugins.indoor(rt, group):
        return None
    key = f"{group}:{language}:{rt.settings.temperature}:{rt.settings.wind}"
    cached = publication_plugins._cached(rt, key, PLUGIN)
    if cached and cached.get("version") == CACHE_VERSION:
        return {
            "line": cached["section"],
            "source": (cached["provenance"] or {}).get("source"),
            "place": place(rt, group),
        }
    try:
        found = publication_plugins._weather(rt, group, language, http=http, recent=True)
    except (requests.RequestException, ValueError):
        return None  # offline or no answer: try again next time
    if not found:
        return None
    line, provenance = found
    source = "garmin" if "garmin" in str(provenance.get("source")) else "open-meteo"
    publication_plugins._save(
        rt, key, PLUGIN, line, {**provenance, "source": source}, CACHE_VERSION
    )
    return {"line": line, "source": source, "place": place(rt, group)}


def _last_outdoor_point(rt) -> tuple[float, float] | None:
    with rt.engine.connect() as conn:
        recent = conn.execute(
            select(activity_unified.c.group_id)
            .order_by(activity_unified.c.start_utc.desc())
            .limit(30)
        ).scalars()
        recent = list(recent)
    for group_id in recent:
        if publication_plugins.indoor(rt, group_id):
            continue
        found = publication_plugins._location(rt, group_id)
        if found:
            return found[0], found[1]
    return None


def location(rt) -> dict[str, Any]:
    """Where the forecast is for: the chosen place, else where you last trained outside."""
    from vibesynergy.portal import prefs

    chosen = prefs.weather_location(rt.settings.data_dir)
    if chosen:
        return {**chosen, "kind": "chosen"}
    point = _last_outdoor_point(rt)
    if point is None:
        return {"kind": "none"}
    return {"kind": "last_outdoor", "latitude": point[0], "longitude": point[1]}


def places(query: str, language: str = "nl", *, http=requests.get) -> list[dict[str, Any]]:
    """Places matching a typed name, for choosing the weather location."""
    text = (query or "").strip()
    if len(text) < 2:
        return []
    try:
        response = http(
            GEOCODING_URL,
            params={"name": text[:80], "count": 8, "language": language, "format": "json"},
            timeout=5,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for item in results:
        try:
            lat, lon = float(item["latitude"]), float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        where = ", ".join(str(x) for x in (item.get("admin1"), item.get("country")) if x)
        name = str(item.get("name") or "").strip()
        if name:
            out.append({"name": name, "where": where, "latitude": lat, "longitude": lon})
    return out


def _flags(rain: float, gust: float | None, low: float | None, snow: float) -> list[str]:
    from vibesynergy.query import weather

    flags = []
    if rain >= weather.RAIN_MM:
        flags.append("rain")
    if snow > 0:
        flags.append("snow")
    if gust is not None and gust >= weather.GUST_MS:
        flags.append("gusts")
    if low is not None and low <= weather.COLD_C:
        flags.append("cold")
    return flags


def forecast(rt, *, http=requests.get, now: float | None = None) -> dict[str, Any]:
    """Fourteen days of Open-Meteo forecast for ``location``, per day in the same shape as
    ``weather.forecast``. Empty without a place or a connection."""
    now = time.monotonic() if now is None else now
    where = location(rt)
    point = (where["latitude"], where["longitude"]) if "latitude" in where else None
    if (
        _FORECAST["value"] is not None
        and _FORECAST["point"] == point
        and now - _FORECAST["at"] < FORECAST_SECONDS
    ):
        return _FORECAST["value"]
    value: dict[str, Any] = {"source": "open-meteo", "days": [], "location": where}
    if point is not None:
        try:
            response = http(
                FORECAST_URL,
                params={
                    "latitude": round(point[0], 2),
                    "longitude": round(point[1], 2),
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max",
                    "wind_speed_unit": "ms",
                    "forecast_days": 14,
                    "timezone": rt.settings.timezone or "auto",
                },
                timeout=5,
            )
            response.raise_for_status()
            daily = response.json().get("daily") or {}
        except (requests.RequestException, ValueError):
            daily = {}

        def col(name: str) -> list:
            return list(daily.get(name) or [])

        codes = col("weather_code")
        for i, day in enumerate(col("time")):
            row = {
                name: (vals[i] if i < len(vals) else None)
                for name, vals in daily.items()
                if isinstance(vals, list)
            }
            pick = row.get
            rain = pick("precipitation_sum") or 0.0
            snow = pick("snowfall_sum") or 0.0
            code = codes[i] if i < len(codes) else None
            value["days"].append(
                {
                    "date": day,
                    "temp_min_c": pick("temperature_2m_min"),
                    "temp_max_c": pick("temperature_2m_max"),
                    "wind_ms": pick("wind_speed_10m_max"),
                    "gust_ms": pick("wind_gusts_10m_max"),
                    "rain_mm": rain,
                    "snow_mm": snow,
                    "summary": publication_format.WMO.get(code) if type(code) is int else None,
                    "flags": _flags(
                        rain, pick("wind_gusts_10m_max"), pick("temperature_2m_min"), snow
                    ),
                }
            )
    _FORECAST.update(at=now, value=value, point=point)
    return value
