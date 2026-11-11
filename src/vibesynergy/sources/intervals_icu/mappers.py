"""intervals.icu calendar events -> ``icu_event`` cache rows.

A workout ``external_id`` is the session key ``<date>:<sport>``. Brick parts add
``:fiets`` (bike) or ``:loop`` (run). Those two parts share the calendar date as
``session_key``, so they stay one session when the sport in the key differs.
An event with no ``external_id`` is the athlete's own, or one intervals.icu created,
and is stored with ``source`` ``other``. Events that have an ``external_id`` are the
plan (``source`` ``plan``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from vibesynergy.model import IcuEvent, Record
from vibesynergy.store.raw import RawRecord

_BRICK_PARTS = frozenset({"fiets", "loop"})
TARGET_KEYS = ("time_target", "load_target", "distance_target")

PLAN = "plan"
OTHER = "other"


def assign_sessions(
    events: list[dict[str, Any]],
) -> dict[str, tuple[str, str | None, str | None]]:
    """Map an event id to ``(source, session_key, brick_part)``."""
    assigned: dict[str, tuple[str, str | None, str | None]] = {}
    for event in events:
        if event.get("id") is None:
            continue
        event_id = str(event["id"])
        external_id = _external_id(event.get("external_id"))
        if not external_id:
            assigned[event_id] = (OTHER, None, None)
            continue
        brick = _brick(external_id)
        if brick is None:
            assigned[event_id] = (PLAN, external_id, None)
        else:
            session_key, part = brick
            assigned[event_id] = (PLAN, session_key, part)
    return assigned


def _external_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _brick(external_id: str) -> tuple[str, str] | None:
    """Return ``(session date, part)`` when ``external_id`` ends in ``:fiets`` or ``:loop``."""
    head, sep, part = external_id.rpartition(":")
    if not sep or part not in _BRICK_PARTS or not head:
        return None
    session_key = head.split(":", 1)[0]
    if not session_key:
        return None
    return session_key, part


def map_events(raw: RawRecord) -> Iterable[Record]:
    payload = raw.json()
    if not isinstance(payload, list):
        raise ValueError("intervals.icu events payload was not a list")
    sessions = assign_sessions([event for event in payload if isinstance(event, dict)])
    for event in payload:
        if not isinstance(event, dict):
            raise ValueError("intervals.icu event was not an object")
        if event.get("id") is None:
            raise ValueError("intervals.icu event is missing id")
        event_id = str(event["id"])
        if not event.get("start_date_local"):
            raise ValueError(f"intervals.icu event {event_id} is missing start_date_local")
        category = event.get("category")
        if not isinstance(category, str) or not category:
            raise ValueError(f"intervals.icu event {event_id} is missing category")
        origin, session_key, brick_part = sessions[event_id]
        external_id = _external_id(event.get("external_id"))
        yield IcuEvent(
            id=event_id,
            category=category,
            date=_day(event["start_date_local"], event_id),
            source=origin,
            fetched_at=raw.fetched_at,
            external_id=external_id,
            session_key=session_key,
            brick_part=brick_part,
            type=_text(event.get("type")),
            name=_text(event.get("name")),
            description=_text(event.get("description")),
            workout_doc=event.get("workout_doc")
            if isinstance(event.get("workout_doc"), dict | list)
            else None,
            tags=_tags(event.get("tags")),
            for_week=bool(event.get("for_week")),
            targets=_targets(event),
            updated=_updated(event.get("updated")),
            raw=dict(event),
        )


def _day(value: Any, event_id: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"intervals.icu event {event_id} has a bad date") from exc


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item).strip()]


def _targets(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event[key] for key in TARGET_KEYS if event.get(key) is not None}


def _updated(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
