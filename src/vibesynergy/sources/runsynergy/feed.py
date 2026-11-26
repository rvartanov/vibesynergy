"""The RunSynergy calendar link: reading it and keeping it.

RunSynergy (the running-group app) gives every coach and runner a private calendar link
(``https://<host>/agenda/<token>.ics``). Each event carries, next to the readable text, one
``X-RUNSYNERGY-DATA`` line with JSON (contract version 1): the club, the role the reader has
there, the kind (``groep``, ``test``, ``wedstrijd``), the date and start time, and the parts
of the training (``warming_up``, ``techniek``, ``kern``, ``cooling_down`` with minutes and
text). VibeSynergy keeps only what it needs for the group trainings the athlete gives.

The link is a secret (anyone with it reads the schedule), so it lives in the encrypted
secret store and the page only ever learns whether one is set and its host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import icalendar

from vibesynergy.core.secrets import SecretStore

STORE = "runsynergy"
DOCUMENT = "feed"
CONTRACT = 1
DATA_PROPERTY = "X-RUNSYNERGY-DATA"
PARTS = ("warming_up", "techniek", "kern", "cooling_down")
_ESCAPE = re.compile(r"\\([\\,;nN])")


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class Training:
    uid: str
    day: date
    start: str | None  # local "HH:MM", None for a date-only event
    sequence: int
    cancelled: bool
    summary: str
    data: dict[str, Any]

    @property
    def club(self) -> str:
        return str(self.data.get("club") or "")

    @property
    def role(self) -> str:
        return str(self.data.get("rol") or "")

    @property
    def kind(self) -> str:
        return str(self.data.get("soort") or "")


def clean_url(url: str) -> str:
    """A calendar link that is safe to store: https (or webcal), a host, a path."""
    text = (url or "").strip()
    if text.startswith("webcal://"):
        text = "https://" + text[len("webcal://") :]
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or not parts.path.strip("/"):
        raise FeedError("Paste the full RunSynergy calendar link, starting with https://")
    if parts.username or parts.password:
        raise FeedError("Paste the calendar link without a user name or password in it")
    return text


def save(secrets_dir: Path, url: str) -> dict[str, Any]:
    SecretStore(secrets_dir, STORE).set(DOCUMENT, {"url": clean_url(url)})
    return status(secrets_dir)


def clear(secrets_dir: Path) -> None:
    SecretStore(secrets_dir, STORE).delete(DOCUMENT)


def link(secrets_dir: Path) -> str | None:
    doc = SecretStore(secrets_dir, STORE).get(DOCUMENT) or {}
    url = doc.get("url") if isinstance(doc, dict) else None
    return url or None


def status(secrets_dir: Path) -> dict[str, Any]:
    """Whether a link is set and on which host; never the link itself."""
    url = link(secrets_dir)
    return {"set": bool(url), "host": urlsplit(url).hostname if url else None}


def _unescape(value: str) -> str:
    def one(match: re.Match) -> str:
        char = match.group(1)
        return "\n" if char in "nN" else char

    return _ESCAPE.sub(one, value)


def data_of(raw: str) -> dict[str, Any] | None:
    """The JSON of an ``X-RUNSYNERGY-DATA`` value. RFC 5545 escapes (``\\,``) are undone
    first when they are there; a value written without them is read as it is."""
    text = raw.strip()
    if re.search(r"\\[,;]", text):
        text = _unescape(text)
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _start(event, zone: ZoneInfo) -> tuple[date, str | None]:
    begin = event.decoded("DTSTART")
    if isinstance(begin, datetime):
        local = begin.astimezone(zone) if begin.tzinfo else begin.replace(tzinfo=zone)
        return local.date(), local.strftime("%H:%M")
    if isinstance(begin, date):
        return begin, None
    raise FeedError("event without a start date")


def parse(ics: str | bytes, timezone: str) -> list[Training]:
    """Every event with contract-1 data, in date order. Events without the data line or
    with another contract version are skipped (a calendar may hold other things)."""
    try:
        calendar = icalendar.Calendar.from_ical(ics)
    except ValueError as exc:
        raise FeedError("The RunSynergy link did not return a calendar") from exc
    zone = ZoneInfo(timezone)
    out: list[Training] = []
    for event in calendar.walk("VEVENT"):
        raw = event.get(DATA_PROPERTY)
        data = data_of(str(raw)) if raw is not None else None
        if not data or data.get("v") != CONTRACT:
            continue
        uid = str(event.get("UID") or "").strip()
        if not uid:
            continue
        try:
            day, start = _start(event, zone)
        except (KeyError, FeedError):
            continue
        if data.get("start") and not start:
            start = str(data["start"])[:5]
        status_text = str(event.get("STATUS") or "CONFIRMED").upper()
        try:
            sequence = int(event.get("SEQUENCE") or 0)
        except (TypeError, ValueError):
            sequence = 0
        out.append(
            Training(
                uid=uid,
                day=day,
                start=start,
                sequence=sequence,
                cancelled=status_text == "CANCELLED" or data.get("afgelast") is True,
                summary=str(event.get("SUMMARY") or ""),
                data=data,
            )
        )
    out.sort(key=lambda t: (t.day, t.start or "", t.uid))
    return out
