"""Measurement gaps in the night's numbers (#88).

Two things went wrong on 17-11-2026 and the advice did not say so:

- **A night that is updated later.** The watch first reported 3 h 29 of sleep; Garmin later
  joined the rest of the night to it (10 h 55). Everything computed in between (a morning
  report, the first readiness) used the short night.
- **A readiness that is not the morning one.** Garmin recomputes readiness during the day.
  The daily value VibeSynergy keeps is the latest reading, so by the evening it was the
  reading after training (11), not the one after waking up (27). A reading from before the
  night was complete is a third kind (3, on the partial night).

``night`` reads the raw history VibeSynergy already keeps (every fetched version of the sleep
and readiness payloads) and reports what it finds. It never writes. The safety floor uses
the morning reading when the daily value is a later one, leaves readiness out when the only
reading is from a partial night, and the advice names the gap.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.store.schema import raw_item

log = logging.getLogger(__name__)

SOURCE = "garmin"
# A later version counts as "the night was updated" when it added at least an hour and the
# first reading was under three quarters of the final night.
UPDATED_MIN_S = 3600
UPDATED_SHARE = 0.75
WAKEUP = "AFTER_WAKEUP_RESET"

_cache: dict[tuple, dict[str, Any] | None] = {}
_CACHE_MAX = 64


def _utc(text: Any) -> datetime | None:
    """A fetched_at (ISO with offset) or Garmin's GMT ``timestamp`` (no offset) as UTC."""
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=UTC)
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _versions(engine: Engine, dataset: str, day: date) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.id, raw_item.c.fetched_at, raw_item.c.is_current)
            .where(
                raw_item.c.source == SOURCE,
                raw_item.c.dataset == dataset,
                raw_item.c.item_key == day.isoformat(),
            )
            .order_by(raw_item.c.fetched_at)
        ).all()
    return [{"id": r[0], "fetched_at": r[1], "current": bool(r[2])} for r in rows]


def _json(raw, item_id: str) -> Any:
    try:
        return raw.load(item_id).json()
    except Exception as exc:  # a missing blob is no reason to lose the advice
        log.debug("measurement: raw item %s could not be read: %s", item_id, exc)
        return None


def _sleep_seconds(payload: Any) -> tuple[float | None, float | None]:
    """(sleep seconds, overall sleep score) from one sleep payload."""
    daily = (payload or {}).get("dailySleepDTO") if isinstance(payload, dict) else None
    if not isinstance(daily, dict):
        return None, None
    seconds = daily.get("sleepTimeSeconds")
    overall = (daily.get("sleepScores") or {}).get("overall") or {}
    score = overall.get("value") if isinstance(overall, dict) else None
    return (
        float(seconds) if isinstance(seconds, (int, float)) else None,
        float(score) if isinstance(score, (int, float)) else None,
    )


def sleep_update(readings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """``readings`` are the night's versions, oldest first: ``{at, seconds}``. Returns the
    update when the night grew by an hour or more after its first reading."""
    known = [r for r in readings if r.get("seconds") is not None]
    if len(known) < 2:
        return None
    first, last = known[0], known[-1]
    grown = last["seconds"] - first["seconds"]
    if grown < UPDATED_MIN_S or first["seconds"] >= last["seconds"] * UPDATED_SHARE:
        return None
    changed = next(r for r in known if r["seconds"] == last["seconds"])
    return {
        "state": "updated",
        "first_s": first["seconds"],
        "now_s": last["seconds"],
        "first_seen_at": _iso(first.get("at")),
        "updated_at": _iso(changed.get("at")),
    }


def _iso(value: Any) -> str | None:
    found = _utc(value)
    return found.isoformat() if found else None


def readiness_reading(
    entries: list[dict[str, Any]],
    *,
    sleep: dict[str, Any] | None = None,
    sleep_score: float | None = None,
) -> dict[str, Any] | None:
    """Which reading the day's readiness is, from Garmin's readings of that day.

    ``context``: ``morning`` (after waking up), ``after_exercise`` or ``later`` (any other
    recomputation during the day). ``morning`` is the score after waking up on the complete
    night, when there is one. ``partial_night`` is true when the only morning reading was
    computed before the night was complete. Returns ``None`` when there is nothing to say:
    the day's value is the morning reading on the complete night.
    """
    scored = [e for e in entries if isinstance(e, dict) and e.get("score") is not None]
    if not scored:
        return None
    latest = max(scored, key=lambda e: str(e.get("timestamp") or ""))
    updated_at = _utc((sleep or {}).get("updated_at"))

    def partial(entry: dict[str, Any]) -> bool:
        seen = entry.get("sleepScore")
        if sleep_score is not None and seen is not None:
            # The reading names the sleep score it was computed on: that decides.
            return float(seen) != sleep_score
        when = _utc(entry.get("timestamp"))
        return bool(updated_at and when and when < updated_at)

    wakeups = sorted(
        (e for e in scored if e.get("inputContext") == WAKEUP),
        key=lambda e: str(e.get("timestamp") or ""),
    )
    complete = [e for e in wakeups if not partial(e)]
    morning = complete[-1] if complete else None
    context = (
        "morning"
        if latest.get("inputContext") == WAKEUP
        else "after_exercise"
        if "EXERCISE" in str(latest.get("inputContext") or "")
        else "later"
    )
    partial_night = morning is None and bool(wakeups) and partial(wakeups[-1])
    if context == "morning" and not partial_night:
        return None
    if context != "morning" and morning is None and not partial_night:
        return None  # no morning reading to fall back on, and nothing known to be wrong
    return {
        "value": float(latest["score"]),
        "context": context,
        "morning": float(morning["score"]) if morning else None,
        "partial_night": partial_night,
    }


def night(engine: Engine, raw, day: date) -> dict[str, Any] | None:
    """The measurement gaps of ``day``'s night: ``{"sleep": …, "readiness": …}`` with only
    the parts that apply, or ``None`` when the numbers can be taken as they are (or there
    is no raw archive to look in)."""
    if raw is None:
        return None
    sleep_versions = _versions(engine, "sleep", day)
    ready_versions = _versions(engine, "training_readiness", day)
    key = (
        day,
        tuple(v["id"] for v in sleep_versions),
        next((v["id"] for v in ready_versions if v["current"]), None),
    )
    if key in _cache:
        return _cache[key]
    readings = []
    score = None
    for version in sleep_versions:
        seconds, found_score = _sleep_seconds(_json(raw, version["id"]))
        readings.append({"at": version["fetched_at"], "seconds": seconds})
        if version["current"]:
            score = found_score
    sleep = sleep_update(readings)
    current = next((v for v in ready_versions if v["current"]), None)
    payload = _json(raw, current["id"]) if current else None
    entries = payload if isinstance(payload, list) else [payload] if payload else []
    readiness = readiness_reading(entries, sleep=sleep, sleep_score=score)
    out: dict[str, Any] | None = None
    if sleep or readiness:
        out = {k: v for k, v in (("sleep", sleep), ("readiness", readiness)) if v}
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = out
    return out
