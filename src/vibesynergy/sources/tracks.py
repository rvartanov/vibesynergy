"""GPX and TCX files: an activity and its per-second stream, like a FIT file gives.

GPX (from Strava, Komoot, old devices) has positions, elevation and time, with heart rate,
cadence and power in the Garmin TrackPointExtension when present. TCX has the same plus
distance, laps and the sport. Distance is computed from the positions when a file has none.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.model import Activity, Record, Stream

GPX_SPORTS = {
    "running": "running",
    "run": "running",
    "cycling": "cycling",
    "biking": "cycling",
    "ride": "cycling",
    "walking": "walking",
    "hiking": "hiking",
    "swimming": "swimming",
}
TCX_SPORTS = {"running": "running", "biking": "cycling", "other": "generic"}


def is_gpx(data: bytes) -> bool:
    return b"<gpx" in data[:2000]


def is_tcx(data: bytes) -> bool:
    return b"TrainingCenterDatabase" in data[:2000]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, *names: str) -> ET.Element | None:
    """First descendant whose local tag name matches (namespaces vary between tools)."""
    for child in el.iter():
        if _local(child.tag) in names:
            return child
    return None


def _num(el: ET.Element | None) -> float | None:
    try:
        return float(el.text) if el is not None and el.text else None
    except ValueError:
        return None


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class NoTimedPoints(ValueError):
    """A route without times (planned, or drawn): no activity to make of it."""


def map_track(data: bytes, source_id: str, *, timezone: str) -> list[Record]:
    # Strava's TCX files start with spaces before the XML declaration, which the parser
    # refuses; a byte order mark is left out the same way.
    root = ET.fromstring(data.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip())
    if _local(root.tag) == "gpx":
        points, sport, name = _gpx(root)
    else:
        points, sport, name = _tcx(root)
    points = [p for p in points if p["t"] is not None]
    if len(points) < 2:
        raise NoTimedPoints("no timed track points")
    points.sort(key=lambda p: p["t"])
    # Distance: the file's when present, else summed from positions.
    if all(p.get("dist") is None for p in points):
        total, prev = 0.0, None
        for p in points:
            if p.get("lat") is not None and p.get("lon") is not None:
                if prev is not None:
                    total += _metres(prev, (p["lat"], p["lon"]))
                prev = (p["lat"], p["lon"])
            p["dist"] = total
    start, end = points[0]["t"], points[-1]["t"]
    alts = [p["alt"] for p in points if p.get("alt") is not None]
    ascent = sum(max(0.0, b - a) for a, b in pairwise(alts)) if alts else None
    hrs = [p["hr"] for p in points if p.get("hr")]
    powers = [p["power"] for p in points if p.get("power") is not None]
    distance = next((p["dist"] for p in reversed(points) if p.get("dist") is not None), None)
    activity = Activity(
        source_id,
        start,
        start.astimezone(ZoneInfo(timezone)).date(),
        sport,
        name=name,
        elapsed_s=(end - start).total_seconds(),
        distance_m=distance,
        ascent_m=round(ascent, 1) if ascent is not None else None,
        avg_hr=round(sum(hrs) / len(hrs), 1) if hrs else None,
        max_hr=max(hrs) if hrs else None,
        avg_power=round(sum(powers) / len(powers), 1) if powers else None,
        fit_id=source_id,
    )
    channels = ("lat", "lon", "alt", "dist", "hr", "cadence", "power")
    columns: dict[str, list[Any]] = {"t": [p["t"] for p in points]}
    for channel in channels:
        values = [p.get(channel) for p in points]
        if any(v is not None for v in values):
            columns[channel] = values
    return [activity, Stream(source_id, columns)]


def _gpx(root: ET.Element) -> tuple[list[dict], str, str | None]:
    track = _find(root, "trk")
    name_el = _find(track, "name") if track is not None else None
    type_el = _find(track, "type") if track is not None else None
    sport = (
        GPX_SPORTS.get((type_el.text or "").strip().lower(), "generic")
        if type_el is not None
        else "generic"
    )
    points = []
    for pt in root.iter():
        if _local(pt.tag) != "trkpt":
            continue
        points.append(
            {
                "t": _time(getattr(_find(pt, "time"), "text", None)),
                "lat": float(pt.get("lat")) if pt.get("lat") else None,
                "lon": float(pt.get("lon")) if pt.get("lon") else None,
                "alt": _num(_find(pt, "ele")),
                "hr": _num(_find(pt, "hr")),
                "cadence": _num(_find(pt, "cad", "cadence")),
                "power": _num(_find(pt, "power", "PowerInWatts")),
            }
        )
    return points, sport, name_el.text if name_el is not None else None


def _tcx(root: ET.Element) -> tuple[list[dict], str, str | None]:
    activity = _find(root, "Activity")
    sport = (
        TCX_SPORTS.get((activity.get("Sport") or "").lower(), "generic")
        if activity is not None
        else "generic"
    )
    notes = _find(activity, "Notes") if activity is not None else None
    points = []
    for tp in root.iter():
        if _local(tp.tag) != "Trackpoint":
            continue
        hr = _find(tp, "HeartRateBpm")
        points.append(
            {
                "t": _time(getattr(_find(tp, "Time"), "text", None)),
                "lat": _num(_find(tp, "LatitudeDegrees")),
                "lon": _num(_find(tp, "LongitudeDegrees")),
                "alt": _num(_find(tp, "AltitudeMeters")),
                "dist": _num(_find(tp, "DistanceMeters")),
                "hr": _num(_find(hr, "Value")) if hr is not None else None,
                "cadence": _num(_find(tp, "Cadence", "RunCadence")),
                "power": _num(_find(tp, "Watts")),
            }
        )
    return points, sport, notes.text if notes is not None else None
