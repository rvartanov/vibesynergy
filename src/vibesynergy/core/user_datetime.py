"""User-facing date and time formatting for the portal, reports and CLI.

Calendar dates and clock times use Dutch numeric notation (``1-12-2026``, ``13:45``)
regardless of UI language. Clock times have no seconds; durations, laps and splits
keep theirs (formatted elsewhere). Month and weekday names use the requested language when needed.
Timestamps are shown in the given IANA zone (default Europe/Amsterdam).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

DISPLAY_TZ = "Europe/Amsterdam"

_MONTH_SHORT = {
    "en": (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ),
    "nl": (
        "jan",
        "feb",
        "mrt",
        "apr",
        "mei",
        "jun",
        "jul",
        "aug",
        "sep",
        "okt",
        "nov",
        "dec",
    ),
}

_WEEKDAY_SHORT = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "nl": ("ma", "di", "wo", "do", "vr", "za", "zo"),
}


def _lang(language: str) -> str:
    return "nl" if language.startswith("nl") else "en"


def format_user_date(value: date) -> str:
    return f"{value.day}-{value.month}-{value.year}"


def format_user_time(local: datetime) -> str:
    return local.strftime("%H:%M")


def format_user_datetime(value: datetime, timezone: str = DISPLAY_TZ) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(ZoneInfo(timezone))
    return f"{format_user_date(local.date())} {format_user_time(local)}"


def format_user_month_year(value: date, language: str = "en") -> str:
    lang = _lang(language)
    return f"{_MONTH_SHORT[lang][value.month - 1]} {value.year}"


def format_user_short_day(value: date, language: str = "en") -> str:
    """Short weekday, day and month: "zo 1 nov" or "Sun 1 Nov" (mail subjects)."""
    lang = _lang(language)
    weekday = _WEEKDAY_SHORT[lang][value.weekday()]
    return f"{weekday} {value.day} {_MONTH_SHORT[lang][value.month - 1]}"


def format_user_local_time_hhmm(local: datetime) -> str:
    """Activity-local clock for API fields (HH:MM, no seconds)."""
    return local.strftime("%H:%M")
