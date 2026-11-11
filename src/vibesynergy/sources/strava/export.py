"""Strava account archive (strava.com > Settings > My Account > Download or delete your
account > Request your archive): history without the API's daily limits.

``vibesynergy import strava-export <zip>`` stores ``activities.csv`` and every original activity
file (FIT, GPX or TCX, mostly gzipped) in the raw archive under source ``strava_export``. The
CSV gives one activity per row, keyed by the Strava activity id, so it lines up with the API's
copy (``strava``, the same id) and, by time, with Garmin's. Each original file becomes that
activity's stream; a file already archived through another route (the FIT that Garmin
uploaded to Strava) is skipped by content hash.

The archive isn't documented either. Several CSV columns appear twice, first rounded in the
athlete's units, later precise in metres and seconds: the last one wins. Dates are UTC in a
human format that follows the account's language. What a mapper doesn't recognise stays in
the raw archive; after a fix ``vibesynergy normalise strava_export`` re-maps it.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.timeutil import Window
from vibesynergy.fit.decode import is_fit, map_fit
from vibesynergy.model import Activity, Record, Stream
from vibesynergy.sources.base import DatasetKind, DatasetSpec, Mapper, Source
from vibesynergy.sources.tracks import NoTimedPoints, is_gpx, is_tcx, map_track
from vibesynergy.store.raw import RawRecord

CSV_NAME = "activities.csv"


def stream_key(activity_id: str) -> str:
    """The stream of an activity's original file (the API's streams are ``strava-<id>``)."""
    return f"strava-export-{activity_id}"


class StravaExportSource(Source):
    id = "strava_export"
    name = "Strava account archive (import only)"

    def datasets(self) -> Sequence[DatasetSpec]:
        return [
            DatasetSpec("activities", DatasetKind.FILE, "activities.csv from the archive"),
            DatasetSpec("files", DatasetKind.FILE, "Original activity files, by content hash"),
        ]

    def fetch(self, dataset: DatasetSpec, window: Window | None):
        return iter(())  # import only

    def mappers(self) -> Mapping[str, Mapper]:
        timezone = self.ctx.settings.timezone

        def activities(raw: RawRecord) -> Iterable[Record]:
            return map_activities(raw.payload, timezone)

        def files(raw: RawRecord) -> Iterable[Record]:
            return map_file(raw.payload, str(raw.request.get("activity_id") or ""), timezone)

        return {"activities": activities, "files": files}


# activities.csv ------------------------------------------------------------------------------


def rows(data: bytes) -> Iterator[dict[str, str]]:
    """The CSV's rows as {column: value}; a column that appears twice keeps its last value
    (the precise one). Column names are compared case-insensitively."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    header = [h.strip().lower() for h in next(reader, [])]
    if "activity id" not in header:
        raise ValueError(f"{CSV_NAME} has no 'Activity ID' column; columns: {header[:8]}")
    for values in reader:
        if values:
            yield {name: value.strip() for name, value in zip(header, values, strict=False)}


def files_by_activity(data: bytes) -> dict[str, str]:
    """{file name in the archive: activity id} for the activities that have an original file."""
    return {r["filename"]: r["activity id"] for r in rows(data) if r.get("filename")}


def map_activities(data: bytes, timezone: str) -> Iterator[Record]:
    header = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    names = [h.strip().lower() for h in next(header, [])]
    # Older archives have one Distance column, in kilometres; newer ones add one in metres.
    distance_scale = 1.0 if names.count("distance") > 1 else 1000.0
    zone = ZoneInfo(timezone)
    for r in rows(data):
        activity_id, start = r.get("activity id"), parse_when(r.get("activity date", ""))
        if not activity_id or start is None:
            continue
        distance = _num(r.get("distance"))
        extra = {
            key: r[column] for key, column in _EXTRA.items() if r.get(column) not in (None, "")
        }
        yield Activity(
            source_id=activity_id,
            start_utc=start,
            local_date=start.astimezone(zone).date(),
            sport=_sport(r.get("activity type")),
            name=r.get("activity name") or None,
            elapsed_s=_num(r.get("elapsed time")),
            moving_s=_num(r.get("moving time")),
            distance_m=distance * distance_scale if distance is not None else None,
            ascent_m=_num(r.get("elevation gain")),
            avg_hr=_num(r.get("average heart rate")),
            max_hr=_num(r.get("max heart rate")),
            avg_power=_num(r.get("average watts")),
            norm_power=_num(r.get("weighted average power")),
            avg_speed=_num(r.get("average speed")),
            calories=_num(r.get("calories")),
            fit_id=stream_key(activity_id) if r.get("filename") else None,
            extra=extra,
        )


_EXTRA = {
    "description": "activity description",
    "private_note": "activity private note",
    "gear": "activity gear",
    "commute": "commute",
    "filename": "filename",
    "relative_effort": "relative effort",
}


def _sport(text: str | None) -> str:
    # "Trail Run" -> "TrailRun", as the API's sport_type.
    return "".join((text or "").split()) or "unknown"


def _num(text: str | None) -> float | None:
    if not text:
        return None
    text = text.replace(" ", "")
    if "," in text and "." not in text:  # a decimal comma
        text = text.replace(",", ".")
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "mrt": 3, "maa": 3, "apr": 4, "may": 5, "mei": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12,
}  # fmt: skip


def parse_when(text: str) -> datetime | None:
    """The CSV's 'Activity Date' (UTC): 'Sep 27, 2026, 8:03:21 AM', '27 Sep 2026, 08:03:21',
    '27 sep. 2026 08:03:21' or ISO."""
    text = text.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    tokens = re.findall(r"[^\W\d_]+|\d+", text.lower())
    month = next((_MONTHS[t[:3]] for t in tokens if t[:3] in _MONTHS), None)
    numbers = [int(t) for t in tokens if t.isdigit()]
    year = next((n for n in numbers if n > 1900), None)
    if month is None or year is None or len(numbers) < 4:
        return None
    rest = numbers.copy()
    rest.remove(year)
    day, clock = rest[0], rest[1:4]
    hour, minute, second = [*clock, 0, 0, 0][:3]
    if "pm" in tokens and hour < 12:
        hour += 12
    elif "am" in tokens and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


# Original files ------------------------------------------------------------------------------


def map_file(data: bytes, activity_id: str, timezone: str) -> list[Record]:
    """An activity's original file: its stream, under the key the CSV row points to. A FIT
    file also brings its file record (device) and zones; its own activity row isn't needed,
    the CSV has one."""
    key = stream_key(activity_id) if activity_id else None
    if is_fit(data):
        # Namespaced, so a Strava id can never be taken for a Garmin activity id.
        linked = f"strava:{activity_id}" if activity_id else None
        records: list[Any] = map_fit(data, timezone=timezone, source_activity_id=linked)
    elif is_gpx(data) or is_tcx(data):
        try:
            track = map_track(data, key or "strava-export", timezone=timezone)
        except NoTimedPoints:
            return []  # a route without times: the CSV row is all there is
        records = [r for r in track if not isinstance(r, Activity)]
    else:
        return []
    if key is None:
        return [r for r in records if not isinstance(r, Stream)]
    return [replace(r, fit_id=key) if isinstance(r, Stream) else r for r in records]
