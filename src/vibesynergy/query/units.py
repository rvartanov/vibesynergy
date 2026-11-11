"""Garmin activity type presentation; stored measurements remain in SI units."""

from __future__ import annotations

import math


def family(sport: str | None) -> str:
    key = (sport or "").lower()
    if "swim" in key:
        return "pool_swim" if "pool" in key or "lap" in key else "open_water_swim"
    if any(word in key for word in ("run", "walk", "hik")):
        return "run"
    if any(word in key for word in ("cycl", "bik", "ride")):
        return "cycle"
    if "row" in key:
        return "row"
    if any(
        word in key
        for word in (
            "strength",
            "yoga",
            "pilates",
            "breath",
            "fitness",
            "training",
            "meditat",
            "cardio",
            "hiit",
            "elliptical",
        )
    ):
        return "time"
    return "other"


def pace_unit(sport: str | None) -> str | None:
    return {
        "run": "/km",
        "pool_swim": "/100m",
        "open_water_swim": "/100m",
        "row": "/500m",
        "cycle": "km/h",
    }.get(family(sport))


def speed_text(sport: str | None, speed: float | None) -> str | None:
    unit = pace_unit(sport)
    if not unit or not isinstance(speed, int | float) or not math.isfinite(speed) or speed <= 0:
        return None
    if unit == "km/h":
        return f"{speed * 3.6:.1f} km/h"
    metres = 1000 if unit == "/km" else 500 if unit == "/500m" else 100
    seconds = round(metres / speed)
    return f"{seconds // 60}:{seconds % 60:02d}{unit}"


def presentation(sport: str | None, row: dict, *, weight_kg: float | None = None) -> dict:
    """Additive, human-readable fields for MCP and assessment inputs."""
    kind = family(sport)
    out = {"family": kind}
    seconds = row.get("moving_s") or row.get("elapsed_s") or row.get("seconds")
    if isinstance(seconds, int | float) and math.isfinite(seconds) and seconds >= 0:
        total = round(seconds)
        out["duration"] = (
            f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
            if total >= 3600
            else f"{total // 60}:{total % 60:02d}"
        )
    distance = row.get("distance_m")
    if isinstance(distance, int | float) and math.isfinite(distance) and distance >= 0:
        out["distance"] = (
            f"{distance:.0f} m"
            if kind in ("pool_swim", "open_water_swim", "row")
            else f"{distance / 1000:.2f} km"
        )
    value = speed_text(sport, row.get("avg_speed"))
    if value:
        out["pace_or_speed"] = value
    power = row.get("avg_power")
    if kind == "cycle" and isinstance(power, int | float):
        out["power"] = f"{power:.0f} W"
        if weight_kg and weight_kg > 0:
            out["power_per_kg"] = f"{power / weight_kg:.2f} W/kg"
    extra = row.get("extra") or {}
    for source, target in (
        ("numberOfActiveLengths", "lengths"),
        ("poolLength", "pool_length_m"),
        ("averageSWOLF", "swolf"),
        ("averageSwolf", "swolf"),
        ("averageStrokeRate", "stroke_rate"),
        ("averageSwimCadence", "stroke_rate"),
        ("averageStrokes", "strokes"),
        ("restDuration", "rest_s"),
        ("repetitions", "reps"),
        ("sets", "sets"),
    ):
        if extra.get(source) is not None:
            out[target] = extra[source]
    if kind == "pool_swim" and isinstance(extra.get("lengthDTOs"), list):
        lengths = [item for item in extra["lengthDTOs"] if isinstance(item, dict)]
        active = [item for item in lengths if item.get("intensityType") != "REST"]
        rest = [item for item in lengths if item.get("intensityType") == "REST"]
        if "lengths" not in out and active:
            out["lengths"] = len(active)
        if rest and "rest_s" not in out:
            durations = [item.get("duration") for item in rest]
            if all(isinstance(value, int | float) and math.isfinite(value) for value in durations):
                out["rest_s"] = sum(durations)
    if kind == "pool_swim" and extra.get("intensityType") == "REST":
        out["rest_s"] = extra.get("restDuration") or row.get("elapsed_s")
    return out
