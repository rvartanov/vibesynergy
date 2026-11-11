"""Deterministic all-day mirrors compatible with the retired Garmin calendar engine.

Ownership is the legacy UID, resource name and ``X-HERMES-*`` proof. Titles and dates
never establish it. These events are single, non-recurring and date-only, which is the
only shape ``parse_object`` accepts as a verified mirror.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from urllib.parse import urljoin

from vibesynergy.query import evidence
from vibesynergy.sources.caldav.calendar import LEGACY_SOURCE

PRODID = "-//VibeSynergy//Calendar mirror//EN"


def digest(origin_id: str) -> str:
    return hashlib.sha256(origin_id.encode()).hexdigest()


def uid_for(origin_id: str) -> str:
    return f"{digest(origin_id)}@{LEGACY_SOURCE}"


def href_for(calendar_url: str, origin_id: str) -> str:
    return urljoin(calendar_url, digest(origin_id) + ".ics")


def summary_text(title: str | None) -> str:
    return " ".join((title or "").split())


def content_fingerprint(title: str | None, start: date, end: date) -> str:
    return evidence.digest(
        {"summary": summary_text(title), "start": start.isoformat(), "end": end.isoformat()}
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def render_event(
    origin_id: str,
    day: date,
    title: str | None,
    *,
    sequence: int,
    stamp: str,
) -> bytes:
    """One VCALENDAR object. ``stamp`` is a UTC compact timestamp (``YYYYMMDDTHHMMSSZ``)."""
    if sequence < 0 or not stamp.endswith("Z"):
        raise ValueError("Mirror sequence and stamp are invalid")
    end = day + timedelta(days=1)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VEVENT",
        f"UID:{uid_for(origin_id)}",
        f"X-HERMES-SOURCE:{LEGACY_SOURCE}",
        f"X-HERMES-KEY:{origin_id}",
        f"DTSTAMP:{stamp}",
        f"SEQUENCE:{sequence}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
    ]
    summary = summary_text(title)
    if summary:
        lines.append(f"SUMMARY:{_escape(summary)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines).encode()
