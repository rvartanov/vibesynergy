"""The public activity-description format: Garmin stats, weather and a quote.

Every Garmin line comes from the activity's own Garmin record; a line whose value is
missing or not a finite number is left out, never estimated. An activity that never got a
Garmin copy gets the same block filled from its own Strava record instead: Relative Effort,
work and temperature, and RPE/feel when intervals.icu or VibeSynergy has them. Section names
are Dutch by the athlete's decision (3 Oct 2026); English labels exist for an English portal.
"""

from __future__ import annotations

import math
import re

from sqlalchemy import select

from vibesynergy.core.config import ConfigError
from vibesynergy.query import activity_effort, views
from vibesynergy.store.schema import activity, activity_unified

DEFAULT_OPENER = "[/*]"
DEFAULT_CLOSER = "[ http://vibesynergy.nl/ */]"  # ; older closers still match
LEGACY_OPENER = "[VibeSynergy]"
LEGACY_CLOSER = "[/VibeSynergy]"
_OPENER_TOKEN = re.compile(r"^\[/\*.*\]$")
_CLOSER_TOKEN = re.compile(r"^\[.*\*/\]$")
_LABEL_FORBIDDEN = re.compile(r"[\[\]:]|\n")

LABELS = {
    "nl": {
        "insight": "Persoonlijke AI inzicht",
        "stats": "Garmin stats",
        "quote": "Quote",
        "aerobic_te": "Aerobe trainingseffect",
        "anaerobic_te": "Anaëroob trainingseffect",
        "te_label": "Effect-label",
        "training_load": "Training load",
        "stamina": "Stamina",
        "temperature": "Temperatuur",
        "average": "gem",
        "feel": "Uitvoeringsgevoel (feel)",
        "rpe": "RPE",
        "relative_effort": "Relatieve inspanning",
        "work": "Arbeid",
    },
    "en": {
        "insight": "Personal AI insight",
        "stats": "Garmin stats",
        "quote": "Quote",
        "aerobic_te": "Aerobic training effect",
        "anaerobic_te": "Anaerobic training effect",
        "te_label": "Effect label",
        "training_load": "Training load",
        "stamina": "Stamina",
        "temperature": "Temperature",
        "average": "avg",
        "feel": "Workout feel",
        "rpe": "RPE",
        "relative_effort": "Relative Effort",
        "work": "Work",
    },
}
EMOJI = {
    "aerobic_te": "🔥",
    "anaerobic_te": "⚡",
    "te_label": "🏷️",
    "training_load": "📊",
    "stamina": "🔋",
    "temperature": "🌡️",
    "feel": "😌",
    "rpe": "😄",
    "relative_effort": "❤️",
    "work": "⚙️",
}
# Garmin weatherTypeDTO.desc -> (emoji, nl, en). Unknown descriptions keep Garmin's own text.
CONDITIONS = {
    "sunny": ("☀️", "Zonnig", "Sunny"),
    "clear": ("☀️", "Helder", "Clear"),
    "fair": ("🌤️", "Licht bewolkt", "Fair"),
    "mostly sunny": ("🌤️", "Overwegend zonnig", "Mostly sunny"),
    "mostly clear": ("🌤️", "Overwegend helder", "Mostly clear"),
    "partly cloudy": ("⛅", "Half bewolkt", "Partly cloudy"),
    "mostly cloudy": ("☁️", "Zwaar bewolkt", "Mostly cloudy"),
    "cloudy": ("☁️", "Bewolkt", "Cloudy"),
    "overcast": ("☁️", "Bewolkt", "Overcast"),
    "fog": ("🌫️", "Mist", "Fog"),
    "haze": ("🌫️", "Nevel", "Haze"),
    "mist": ("🌫️", "Nevel", "Mist"),
    "drizzle": ("🌦️", "Motregen", "Drizzle"),
    "light rain": ("🌦️", "Lichte regen", "Light rain"),
    "showers": ("🌦️", "Buien", "Showers"),
    "rain": ("🌧️", "Regen", "Rain"),
    "heavy rain": ("🌧️", "Zware regen", "Heavy rain"),
    "thunderstorm": ("⛈️", "Onweer", "Thunderstorm"),
    "thunderstorms": ("⛈️", "Onweer", "Thunderstorms"),
    "snow": ("🌨️", "Sneeuw", "Snow"),
    "light snow": ("🌨️", "Lichte sneeuw", "Light snow"),
    "sleet": ("🌨️", "Natte sneeuw", "Sleet"),
    "hail": ("🌨️", "Hagel", "Hail"),
    "windy": ("💨", "Winderig", "Windy"),
}
# WMO weather codes (Open-Meteo fallback) -> CONDITIONS key.
WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "drizzle",
    57: "drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "rain",
    67: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "snow",
    77: "snow",
    80: "showers",
    81: "showers",
    82: "heavy rain",
    85: "snow",
    86: "snow",
    95: "thunderstorm",
    96: "thunderstorm",
    99: "thunderstorm",
}
# RPE and feel on the intervals.icu scales (activity_effort): RPE 1-10, feel 1 Strong .. 5 Weak.
EFFORT_LABELS = {
    "en": {"rpe": activity_effort.RPE_LABELS, "feel": activity_effort.FEEL_LABELS},
    "nl": {
        "rpe": {
            1: "Helemaal niets",
            2: "Heel licht",
            3: "Licht",
            4: "Comfortabel",
            5: "Iets uitdagend",
            6: "Moeilijk",
            7: "Zwaar",
            8: "Heel zwaar",
            9: "Extreem zwaar",
            10: "Maximaal",
        },
        "feel": {1: "Sterk", 2: "Goed", 3: "Normaal", 4: "Matig", 5: "Zwak"},
    },
}
COMPASS_NL = {"n": "N", "e": "O", "s": "Z", "w": "W"}
POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def opener_closer(publication: dict) -> tuple[str, str]:
    return (
        publication.get("opener") or DEFAULT_OPENER,
        publication.get("closer") or DEFAULT_CLOSER,
    )


def wrap_owned(body: str, publication: dict) -> str:
    opener, closer = opener_closer(publication)
    return f"{opener}\n{body}\n{closer}"


def section_labels(publication: dict, language: str) -> dict[str, str]:
    tr = dict(LABELS[language])
    for key, cfg_key in (
        ("stats", "label_stats"),
        ("quote", "label_quote"),
    ):
        custom = publication.get(cfg_key)
        if isinstance(custom, str) and custom.strip():
            tr[key] = custom.strip()
    return tr


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def same_text(left: str | None, right: str | None) -> bool:
    """True when two descriptions differ only by CR/LF spelling."""
    return _normalize_newlines(left or "") == _normalize_newlines(right or "")


def _line_starts(text: str) -> list[tuple[int, int]]:
    """(line_start, line_end) in the original string; line_end is exclusive.

    A line may end in ``\\n``, ``\\r\\n`` or a bare ``\\r``. Offsets stay in the
    caller's text so a later slice does not shift when the provider uses CRLF.
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    limit = len(text)
    while index < limit:
        char = text[index]
        if char == "\n":
            spans.append((start, index + 1))
            index += 1
            start = index
        elif char == "\r":
            end = index + 2 if index + 1 < limit and text[index + 1] == "\n" else index + 1
            spans.append((start, end))
            index = end
            start = index
        else:
            index += 1
    spans.append((start, limit))
    return spans


def _stripped_line(text: str, start: int, end: int) -> str:
    return text[start:end].rstrip("\r\n").strip()


def _is_opener_line(stripped: str) -> bool:
    if _CLOSER_TOKEN.match(stripped):
        return False
    return bool(_OPENER_TOKEN.match(stripped)) or stripped == LEGACY_OPENER


def _is_closer_line(stripped: str) -> bool:
    return bool(_CLOSER_TOKEN.match(stripped)) or stripped == LEGACY_CLOSER


def find_blocks(text: str) -> list[tuple[int, int]]:
    """Character spans of whole-line VibeSynergy blocks (opener through closer).

    Spans index the original string, including its own newline bytes.
    """
    lines = _line_starts(text)
    blocks: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        start, end = lines[index]
        stripped = _stripped_line(text, start, end)
        if not _is_opener_line(stripped):
            index += 1
            continue
        for j in range(index + 1, len(lines)):
            c_start, c_end = lines[j]
            if _is_closer_line(_stripped_line(text, c_start, c_end)):
                blocks.append((start, c_end))
                index = j + 1
                break
        else:
            index += 1
    return blocks


def block_text(text: str, span: tuple[int, int]) -> str:
    """Owned block with newlines normalized, for comparison with the ledger."""
    return _normalize_newlines(text[span[0] : span[1]]).rstrip("\n")


def orphan_markers(text: str) -> bool:
    """True when a marker line sits outside a detected block."""
    if not text:
        return False
    lines = _line_starts(text)
    covered: set[int] = set()
    for start, end in find_blocks(text):
        for line_start, _line_end in lines:
            if start <= line_start < end:
                covered.add(line_start)
    for line_start, line_end in lines:
        stripped = _stripped_line(text, line_start, line_end)
        if (_is_opener_line(stripped) or _is_closer_line(stripped)) and line_start not in covered:
            return True
    return False


def validate_opener(value: object) -> None:
    if not isinstance(value, str):
        raise ConfigError("[publication] opener must be a single-line string")
    if "\n" in value or "\r" in value:
        raise ConfigError("[publication] opener must be a single line")
    if len(value) > 100:
        raise ConfigError("[publication] opener must be at most 100 characters")
    if not value.startswith("[/*") or not value.endswith("]"):
        raise ConfigError("[publication] opener must start with [/* and end with ]")
    if value.endswith("*/]") or _CLOSER_TOKEN.match(value):
        raise ConfigError("[publication] opener must not end with */]")
    if not _OPENER_TOKEN.match(value):
        raise ConfigError("[publication] opener has an invalid marker shape")


def validate_closer(value: object) -> None:
    if not isinstance(value, str):
        raise ConfigError("[publication] closer must be a single-line string")
    if "\n" in value or "\r" in value:
        raise ConfigError("[publication] closer must be a single line")
    if len(value) > 100:
        raise ConfigError("[publication] closer must be at most 100 characters")
    if not value.startswith("[") or not value.endswith("*/]"):
        raise ConfigError("[publication] closer must start with [ and end with */]")
    if not _CLOSER_TOKEN.match(value):
        raise ConfigError("[publication] closer has an invalid marker shape")
    if _OPENER_TOKEN.match(value):
        raise ConfigError("[publication] closer must not use the opener marker shape")


def validate_label(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"[publication] {name} must be a single-line string")
    if "\n" in value or "\r" in value:
        raise ConfigError(f"[publication] {name} must be a single line")
    if len(value) > 40:
        raise ConfigError(f"[publication] {name} must be at most 40 characters")
    if _LABEL_FORBIDDEN.search(value):
        raise ConfigError(f"[publication] {name} must not contain [, ], : or a newline")


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _label(value) -> str | None:
    if not isinstance(value, str):
        return None
    # Garmin's enum (AEROBIC_BASE, RECOVERY, VO2MAX, ...); never free text.
    value = value.strip()
    return value if re.fullmatch(r"[A-Z0-9_]{1,40}", value) else None


def garmin_fields(rt, activity_id: str) -> dict:
    """The Garmin numbers for the activity's single Garmin record ({} without one)."""
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
        )
        ids = [ref.split(":", 1)[1] for ref in sources or [] if ref.startswith("garmin:")]
        if len(ids) != 1:
            return {}
        row = (
            conn.execute(
                select(activity).where(
                    activity.c.source == "garmin", activity.c.source_id == ids[0]
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return {}
    extra = row["extra"] or {}
    summary, detail, weather = {}, {}, None
    if row["raw_item_id"]:
        try:
            raw = rt.raw.load(row["raw_item_id"])
            if raw.content_type == "application/json" and len(raw.payload) <= 2_000_000:
                data = raw.json() or {}
                summary = data.get("summary") or {}
                detail = (data.get("activity") or {}).get("summaryDTO") or {}
                weather = data.get("weather")
        except (OSError, ValueError):
            pass

    def first(*values):
        return next((v for v in (number(x) for x in values) if v is not None), None)

    return {
        "aerobic_te": first(row["aerobic_te"], summary.get("aerobicTrainingEffect")),
        "anaerobic_te": first(row["anaerobic_te"], summary.get("anaerobicTrainingEffect")),
        "te_label": _label(
            summary.get("trainingEffectLabel")
            or detail.get("trainingEffectLabel")
            or extra.get("trainingEffectLabel")
        ),
        "training_load": first(row["training_load"], summary.get("activityTrainingLoad")),
        "stamina_start": first(detail.get("beginPotentialStamina")),
        "stamina_end": first(detail.get("endPotentialStamina")),
        "temp_min": first(
            detail.get("minTemperature"), summary.get("minTemperature"), extra.get("minTemperature")
        ),
        "temp_max": first(
            detail.get("maxTemperature"), summary.get("maxTemperature"), extra.get("maxTemperature")
        ),
        "temp_avg": first(detail.get("averageTemperature"), summary.get("averageTemperature")),
        "feel": first(detail.get("directWorkoutFeel"), extra.get("feel")),
        "rpe": first(detail.get("directWorkoutRpe"), extra.get("rpe")),
        "weather": weather if isinstance(weather, dict) else None,
    }


def has_garmin(rt, activity_id: str) -> bool:
    """True when the unified activity has a Garmin copy (its stats come from there), and
    for an activity that cannot be found (nothing to take from Strava either)."""
    try:
        group = views.resolve_activity(rt.engine, activity_id)
    except (LookupError, ValueError):
        return True
    with rt.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
        )
    return any(str(ref).startswith(("garmin:", "garmin_export:")) for ref in sources or [])


def strava_fields(rt, activity_id: str) -> dict:
    """The Strava numbers for an activity without a Garmin copy ({} without one Strava
    record). RPE and feel are on the intervals.icu scales, from intervals.icu or VibeSynergy."""
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
        )
        ids = [ref.split(":", 1)[1] for ref in sources or [] if ref.startswith("strava:")]
        if len(ids) != 1:
            return {}
        row = (
            conn.execute(
                select(activity).where(
                    activity.c.source == "strava", activity.c.source_id == ids[0]
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return {}
    extra = row["extra"] or {}
    detail = {}
    if row["raw_item_id"]:
        try:
            raw = rt.raw.load(row["raw_item_id"])
            if raw.content_type == "application/json" and len(raw.payload) <= 2_000_000:
                data = raw.json() or {}
                detail = {**(data.get("summary") or {}), **(data.get("detail") or {})}
        except (OSError, ValueError):
            pass
    try:
        effort = activity_effort.for_activity(rt.engine, rt.raw, group)
    except (LookupError, ValueError):
        effort = {}
    return {
        "relative_effort": number(extra.get("suffer_score", detail.get("suffer_score"))),
        "work_kj": number(extra.get("kilojoules", detail.get("kilojoules"))),
        "temp_avg": number(detail.get("average_temp")),
        "icu_rpe": number((effort.get("rpe") or {}).get("value")),
        "icu_feel": number((effort.get("feel") or {}).get("value")),
    }


def strava_stats_lines(fields: dict, language: str = "nl") -> list[str]:
    """One line per available Strava field, in a fixed order; missing data -> no line."""
    tr = LABELS[language]
    effort = EFFORT_LABELS[language]
    fields = {k: number(v) for k, v in fields.items()}
    lines = []

    def add(key, text):
        lines.append(f"{EMOJI[key]} {tr[key]}: {text}")

    if (v := fields.get("relative_effort")) is not None and 0 <= v <= 2000:
        add("relative_effort", _deg(v))
    if (v := fields.get("work_kj")) is not None and 0 < v <= 50_000:
        add("work", f"{_deg(v)} kJ")
    if (v := fields.get("temp_avg")) is not None and -60 <= v <= 60:
        add("temperature", f"{tr['average']} {_deg(v)}°C")
    if (v := fields.get("icu_feel")) is not None and round(v) in effort["feel"]:
        add("feel", effort["feel"][round(v)])
    if (v := fields.get("icu_rpe")) is not None and round(v) in effort["rpe"]:
        add("rpe", f"{round(v)}/10 ({effort['rpe'][round(v)]})")
    return lines


def _one(value: float) -> str:
    return f"{value:.1f}"


def _deg(value: float) -> str:
    return f"{round(value):d}"


def clean(fields: dict) -> dict:
    """Only finite numbers and Garmin's label enum; weather and anything else dropped."""
    out = {k: number(v) for k, v in fields.items() if k not in ("weather", "te_label")}
    out["te_label"] = _label(fields.get("te_label"))
    return {k: v for k, v in out.items() if v is not None}


def stats_lines(fields: dict, language: str = "nl") -> list[str]:
    """One line per available field, in the fixed order; missing data -> no line."""
    tr = LABELS[language]
    lines = []
    fields = clean(fields)

    def add(key, text):
        lines.append(f"{EMOJI[key]} {tr[key]}: {text}")

    for key in ("aerobic_te", "anaerobic_te"):
        if (v := fields.get(key)) is not None and 0 <= v <= 5:
            add(key, _one(v))
    if fields.get("te_label"):
        add("te_label", fields["te_label"])
    if (v := fields.get("training_load")) is not None and v >= 0:
        add("training_load", _one(v))
    start, end = fields.get("stamina_start"), fields.get("stamina_end")
    if start is not None and end is not None and 0 <= start <= 100 and 0 <= end <= 100:
        add("stamina", f"{_deg(start)} -> {_deg(end)}")
    low, high, avg = fields.get("temp_min"), fields.get("temp_max"), fields.get("temp_avg")
    if low is not None and high is not None and low <= high:
        sep = " \u2013 " if low < 0 or high < 0 else "-"
        text = f"{_deg(low)}{sep}{_deg(high)}°C"
        if avg is not None:
            text += f" ({tr['average']} {_deg(avg)}°C)"
        add("temperature", text)
    elif avg is not None:
        add("temperature", f"{tr['average']} {_deg(avg)}°C")
    # Garmin keeps both on 0-100; shown as intervals.icu does (feel label, RPE n/10 + label).
    effort = EFFORT_LABELS[language]
    if (v := fields.get("feel")) is not None and 0 <= v <= 100:
        feel = activity_effort.garmin_feel(v)
        add("feel", effort["feel"][feel])
    if (v := fields.get("rpe")) is not None and 0 < v <= 100:
        rpe = activity_effort.garmin_rpe(v)
        add("rpe", f"{rpe}/10 ({effort['rpe'][rpe]})")
    return lines


def _condition(key_or_desc: str | None, language: str) -> tuple[str, str] | None:
    if not isinstance(key_or_desc, str) or not key_or_desc.strip():
        return None
    desc = " ".join(key_or_desc.strip().split())
    if not re.fullmatch(r"[A-Za-z][A-Za-z ,-]{0,39}", desc):
        return None
    found = CONDITIONS.get(desc.lower())
    if found is None:
        return "🌡️", desc
    emoji, nl, en = found
    return emoji, nl if language == "nl" else en


def _compass(point: str | None, degrees: float | None, language: str) -> str | None:
    if (
        isinstance(point, str)
        and point
        and all(c in "nesw" for c in point.lower())
        and len(point) <= 3
    ):
        letters = point.lower()
    elif degrees is not None and 0 <= degrees <= 360:
        letters = POINTS[round(degrees / 22.5) % 16].lower()
    else:
        return None
    if language == "nl":
        return "".join(COMPASS_NL[c] for c in letters)
    return letters.upper()


def weather_line(
    condition: str | None,
    temp_c: float | None,
    wind_kmh: float | None,
    wind_point: str | None = None,
    wind_degrees: float | None = None,
    language: str = "nl",
) -> str | None:
    """'☁️ Bewolkt, 11°C, 5 km/h W'; parts without data are left out."""
    found = _condition(condition, language)
    temp_c, wind_kmh = number(temp_c), number(wind_kmh)
    parts = []
    if found:
        parts.append(found[1])
    if temp_c is not None and -60 <= temp_c <= 60:
        parts.append(f"{_deg(temp_c)}°C")
    if wind_kmh is not None and 0 <= wind_kmh <= 300:
        wind = f"{_deg(wind_kmh)} km/h"
        if direction := _compass(wind_point, number(wind_degrees), language):
            wind += f" {direction}"
        parts.append(wind)
    if not parts or (found is None and len(parts) < 2):
        return None
    return f"{found[0] if found else '🌡️'} {', '.join(parts)}"


def garmin_weather(weather: dict | None, language: str = "nl") -> str | None:
    """Garmin's activity weather (°F, mph) as one line."""
    if not isinstance(weather, dict):
        return None
    temp_f = number(weather.get("temp"))
    mph = number(weather.get("windSpeed"))
    return weather_line(
        (weather.get("weatherTypeDTO") or {}).get("desc"),
        None if temp_f is None else (temp_f - 32) * 5 / 9,
        None if mph is None else mph * 1.609344,
        weather.get("windDirectionCompassPoint"),
        weather.get("windDirection"),
        language,
    )


def compose(
    *,
    insight: str | None,
    stats: list[str],
    weather: str | None,
    quote: str | None,
    language: str = "nl",
    labels: dict[str, str] | None = None,
) -> str | None:
    """The public block body; None when there is nothing factual to publish."""
    tr = labels if labels is not None else LABELS[language]
    rows = [*stats, *([weather] if weather else [])]
    if not rows:
        return None
    parts = []
    if insight:
        parts.append(f"-{tr['insight']}: {insight}")
    parts.append(f"-{tr['stats']}:\n" + "\n".join(f"  {row}" for row in rows))
    text = "\n".join(parts)
    if quote:
        text += f"\n\n-{tr['quote']}: {quote}"
    return text
