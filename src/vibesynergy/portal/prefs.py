"""Portal preferences kept on the server, so they are the same on the phone and the laptop: the
menu (order of the sections and their pages, hidden pages), the start page's blocks (order,
hidden ones, the period of the best runs), how the day page is laid out,
how often open pages reload, and where each kind of alert goes. One small JSON file in the
data folder; the page knows the defaults and merges these over them, so an id the page no
longer has is simply ignored.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

FILE = "portal-prefs.json"
ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
MAX_IDS = 64


def path(data_dir: Path) -> Path:
    return data_dir / FILE


def load(data_dir: Path) -> dict[str, Any]:
    try:
        stored = json.loads(path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def save(data_dir: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate and merge ``changes`` (a key set to None goes back to the default)."""
    prefs = load(data_dir)
    # Preferences nothing reads any more; an older file keeps them until the next save.
    for key in RETIRED:
        prefs.pop(key, None)
    for key, value in changes.items():
        check = VALIDATORS.get(key)
        if check is None:
            raise ValueError(f"unknown preference {key!r}")
        if value is None:
            prefs.pop(key, None)
        else:
            prefs[key] = check(value)
    target = path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False, suffix=".tmp") as tmp:
        json.dump(prefs, tmp, indent=2)
    os.replace(tmp.name, target)
    return prefs


def _sports(value: Any) -> list[str]:
    allowed = ("swim", "run", "ride")
    if not isinstance(value, list) or any(v not in allowed for v in value):
        raise ValueError(f"garmin_delivery: a list of {', '.join(allowed)}")
    return sorted(set(value))


def _ids(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_IDS:
        raise ValueError(f"{what}: a list of at most {MAX_IDS} ids")
    for item in value:
        if not isinstance(item, str) or not ID.fullmatch(item):
            raise ValueError(f"{what}: {item!r} is not a page or section id")
    if len(set(value)) != len(value):
        raise ValueError(f"{what}: an id is listed twice")
    return list(value)


def _nav(value: Any) -> dict[str, Any]:
    """{"sections": [ids], "pages": {section: [ids]}, "hidden": [ids]}, all optional."""
    if not isinstance(value, dict) or set(value) - {"sections", "pages", "hidden"}:
        raise ValueError("nav: an object with sections, pages and hidden")
    nav: dict[str, Any] = {}
    if "sections" in value:
        nav["sections"] = _ids(value["sections"], "nav.sections")
    if "hidden" in value:
        nav["hidden"] = _ids(value["hidden"], "nav.hidden")
    if "pages" in value:
        pages = value["pages"]
        if not isinstance(pages, dict) or len(pages) > MAX_IDS:
            raise ValueError("nav.pages: an object of section id -> page ids")
        nav["pages"] = {
            _ids([section], "nav.pages")[0]: _ids(ids, f"nav.pages.{section}")
            for section, ids in pages.items()
        }
    return nav


DAY_VIEWS = ("smart", "widgets", "list")


def _day_view(value: Any) -> str:
    if value not in DAY_VIEWS:
        raise ValueError(f"day_view: one of {', '.join(DAY_VIEWS)}")
    return value


def _refresh_min(value: Any) -> int:
    """Minutes between reloads of an open page that shows data (0: never)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 240:
        raise ValueError("refresh_min: whole minutes from 0 (never) to 240")
    return value


def _horizon_days(value: Any) -> int:
    """Days ahead VibeSynergy keeps on Garmin and checks (/#195): 7 to 14, default 9."""
    if isinstance(value, bool) or not isinstance(value, int) or not 7 <= value <= 14:
        raise ValueError("plan_horizon_days: whole days from 7 to 14")
    return value


def horizon_days(data_dir) -> int:
    value = load(data_dir).get("plan_horizon_days")
    return value if isinstance(value, int) and 7 <= value <= 14 else 9


def _weather_location(value: Any) -> dict[str, Any]:
    """A fixed place for the calendar's weather: a name and its coordinates."""
    if not isinstance(value, dict) or set(value) - {"name", "latitude", "longitude"}:
        raise ValueError("weather_location: name, latitude and longitude")
    name = str(value.get("name") or "").strip()
    try:
        lat, lon = float(value["latitude"]), float(value["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("weather_location: latitude and longitude as numbers") from exc
    if not name or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("weather_location: a name and valid coordinates")
    return {"name": name[:120], "latitude": round(lat, 4), "longitude": round(lon, 4)}


def weather_location(data_dir) -> dict[str, Any] | None:
    value = load(data_dir).get("weather_location")
    return value if isinstance(value, dict) else None


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what}: true or false")
    return value


def _one_of(value: Any, what: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError(f"{what}: one of {', '.join(allowed)}")
    return value


ALERT_CHANNELS = ("portal", "email", "push")
RETIRED_CHANNELS = ("whatsapp",)  # removed 31-10-2026; an old page may still send it


def _alert_routes(value: Any) -> dict[str, list[str]]:
    """{kind: [channels]}: where each kind of alert goes (vibesynergy.alerts.KINDS)."""
    if not isinstance(value, dict) or len(value) > MAX_IDS:
        raise ValueError("alert_routes: an object of alert kind -> channels")
    out = {}
    for kind, channels in value.items():
        _ids([kind], "alert_routes")
        if not isinstance(channels, list) or set(channels) - set(ALERT_CHANNELS + RETIRED_CHANNELS):
            raise ValueError(f"alert_routes.{kind}: channels from {', '.join(ALERT_CHANNELS)}")
        out[kind] = [c for c in ALERT_CHANNELS if c in channels]
    return out


# Periods for best running efforts on the start page; vibesynergy.query.start_page has them.
BEST_RUN_PERIODS = ("last_90d", "last_365d", "this_year", "all")
BEST_SPORTS = ("run", "bike", "swim", "other", "all")

# Colour themes; the portal's theme.js has the same list.
PALETTES = ("vibesynergy", "volt", "stroom", "hartslag")

# The "add approved workouts to Garmin" switch left with the plan flows
# intervals.icu → Garmin.
RETIRED = ("garmin.workout_push",)


def _plan_builder(value: Any) -> dict[str, Any]:
    from vibesynergy.query import plan_builder

    return plan_builder.validate(value)


def _athlete_name(value: Any) -> str:
    """How the coach names the athlete; one to forty characters."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
        raise ValueError("athlete_name: a name of 1 to 40 characters")
    return value.strip()


VALIDATORS = {
    "athlete_name": _athlete_name,
    "nav": _nav,
    "day_view": _day_view,
    "refresh_min": _refresh_min,
    "alert_routes": _alert_routes,
    # The start page's blocks: their order and the ones switched off. The page knows
    # the blocks and their default order; an id it no longer has is ignored.
    "dashboard_order": lambda value: _ids(value, "dashboard_order"),
    "dashboard_hidden": lambda value: _ids(value, "dashboard_hidden"),
    # Blocks folded to their heading, remembered per user.
    "dashboard_collapsed": lambda value: _ids(value, "dashboard_collapsed"),
    "best_runs_period": lambda value: _one_of(value, "best_runs_period", BEST_RUN_PERIODS),
    "best_sport": lambda value: _one_of(value, "best_sport", BEST_SPORTS),
    "language": lambda value: _one_of(value, "language", ("auto", "en", "nl")),
    "palette": lambda value: _one_of(value, "palette", PALETTES),
    # Copy diary notes to intervals.icu wellness comments, only while those are private.
    "intervals_icu.private_notes": lambda value: _bool(value, "intervals_icu.private_notes"),
    # Sports VibeSynergy delivers to Garmin itself: swim first, run and ride later.
    "garmin_delivery": lambda value: _sports(value),
    "plan_horizon_days": _horizon_days,
    # The plan builder's settings: goal, sport, method and options.
    "plan_builder": lambda value: _plan_builder(value),
    # Where the calendar's weather forecast is for; absent = where you last trained outside.
    "weather_location": _weather_location,
}
