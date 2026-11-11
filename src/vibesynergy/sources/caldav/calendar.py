"""Calendar parsing and legacy mirror proof; reading never grants write authority."""

import hashlib
import json
from datetime import UTC, date, datetime, time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from vibesynergy.core.timeutil import Window
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.sources.caldav.client import calendar_url, object_url
from vibesynergy.store.raw import RawRecord

LEGACY_SOURCE = "hermes-garmin-v2"
MAX_OCCURRENCES = 10000


def parse_object(obj: dict, base: str) -> tuple[Calendar, str | None]:
    href = object_url(base, obj["url"])
    calendar = Calendar.from_ical(obj["data"])
    if calendar.name != "VCALENDAR" or any(c.errors for c in calendar.walk()):
        raise ValueError("Malformed iCalendar object")
    events = calendar.walk("VEVENT")
    if not events or any(not event.get("UID") for event in events):
        raise ValueError("Calendar object lacks event UID")
    # A DAV object is one UID series, possibly with recurrence exceptions.
    if len({str(event["UID"]) for event in events}) != 1:
        raise ValueError("Calendar object contains multiple UID series")
    signals = [
        e
        for e in events
        if str(e["UID"]).endswith("@" + LEGACY_SOURCE)
        or "X-HERMES-SOURCE" in e
        or "X-HERMES-KEY" in e
    ]
    if not signals:
        return calendar, None
    if len(events) != 1:
        raise ValueError("Suspicious recurring or multi-event legacy mirror")
    event = events[0]
    key = event.get("X-HERMES-KEY")
    if not isinstance(key, str) or not key.startswith("garmin:"):
        raise ValueError("Invalid legacy mirror key")
    digest = hashlib.sha256(key.encode()).hexdigest()
    if (
        str(event.get("X-HERMES-SOURCE")) != LEGACY_SOURCE
        or str(event["UID"]) != digest + "@" + LEGACY_SOURCE
        or href != urljoin(base, digest + ".ics")
    ):
        raise ValueError("Legacy mirror ownership markers do not match")
    if any(p in event for p in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")):
        raise ValueError("Legacy mirrors cannot be recurring")
    if (
        type(event.decoded("DTSTART", None)) is not date
        or type(event.decoded("DTEND", None)) is not date
        or not isinstance(event.get("SEQUENCE"), int)
        or event["SEQUENCE"] < 0
        or "DTSTAMP" not in event
    ):
        raise ValueError("Invalid legacy mirror date, sequence or stamp")
    return calendar, str(key)


def _stamp(value: date | datetime, zone: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return (
            (value.replace(tzinfo=zone) if value.tzinfo is None else value)
            .astimezone(UTC)
            .isoformat()
        )
    return value.isoformat()


def map_calendars(raw: RawRecord):
    snapshot = raw.json()
    if snapshot.get("complete") is not True:
        raise ValueError("Incomplete calendar snapshot cannot replace planned context")
    window = Window.parse(snapshot["window"])
    zone = ZoneInfo(snapshot["timezone"])
    start = datetime.combine(window.start, time.min, zone)
    seen = set()
    calendar_ids = set()
    for selected in snapshot["calendars"]:
        base = calendar_url(selected["url"])
        if base in calendar_ids:
            raise ValueError("Duplicate selected calendar")
        calendar_ids.add(base)
        for obj in selected["objects"]:
            calendar, mirror_key = parse_object(obj, base)
            series = calendar.walk("VEVENT")
            recurring = any(p in e for e in series for p in ("RRULE", "RDATE", "RECURRENCE-ID"))
            if not calendar.get("X-WR-TIMEZONE"):
                calendar.add("X-WR-TIMEZONE", snapshot["timezone"])
            query = recurring_ical_events.of(calendar, skip_bad_series=False)
            for index, event in enumerate(query.after(start)):
                if index >= MAX_OCCURRENCES:
                    raise ValueError("Calendar recurrence expansion exceeds supported limit")
                beginning = event.decoded("DTSTART")
                stamp = _stamp(beginning, zone)
                local_day = (
                    datetime.fromisoformat(stamp).astimezone(zone).date()
                    if isinstance(beginning, datetime)
                    else beginning
                )
                if local_day > window.end:
                    break
                if str(event.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                finish = event.decoded("DTEND", beginning)
                recurrence = (
                    _stamp(event.decoded("RECURRENCE-ID", beginning), zone)
                    if recurring
                    else "single"
                )
                uid = str(event["UID"])
                identity = (base, uid, recurrence)
                if identity in seen:
                    raise ValueError("Duplicate calendar occurrence identity")
                seen.add(identity)
                key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                yield PlannedItem(
                    "calendar:" + key,
                    local_day,
                    "mirror" if mirror_key else "commitment",
                    title=str(event.get("SUMMARY", "")) or None,
                    extra={
                        "calendar_url": base,
                        "calendar_name": selected.get("name"),
                        "href": obj["url"],
                        "uid": uid,
                        "recurrence_id": recurrence,
                        "etag": obj["etag"],
                        "start": stamp,
                        "end": _stamp(finish, zone),
                        "all_day": type(beginning) is date,
                        "timezone": snapshot["timezone"],
                        "description": str(event.get("DESCRIPTION", "")),
                        "location": str(event.get("LOCATION", "")),
                        "snapshot_at": raw.fetched_at.isoformat(),
                        "mirror_key": mirror_key,
                        "ownership": "verified_legacy_mirror" if mirror_key else "personal",
                        "classification": "mirror"
                        if mirror_key
                        else selected.get("classification", "unclassified"),
                        "counts_as_exercise": False,
                    },
                )
    yield CalendarSpan(window.start, window.end, include_overlapping=True)
