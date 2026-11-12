"""RPE and Feel per activity: what Garmin recorded, or what you enter in VibeSynergy.

Scales (the usual ones, as Garmin's watch asks them):

- RPE 1-10. Garmin stores ``directWorkoutRpe`` as 10-100, so 30 is RPE 3.
- Feel 1-5, where 1 is Strong and 5 is Weak. Garmin stores ``directWorkoutFeel`` as 0-100
  the other way round (100 very strong, 0 very weak), so Feel = 5 - garmin / 25, clamped to
  1-5.

Your entry in VibeSynergy is always possible and wins over Garmin's (Garmin's stays visible
as the other value). Nothing is read from or written to intervals.icu for these (12-11-2026).
intervals.icu activities in the raw archive are still read for one thing: which plan workout
intervals.icu paired an activity with, and its compliance (``icu_for_groups``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import (
    activity,
    activity_effort,
    activity_unified,
    raw_item,
)

log = logging.getLogger(__name__)

SUBJECT = 1
GARMIN = "garmin"
ICU = "intervals_icu"
VIBESYNERGY = "vibesynergy"

# intervals.icu's own labels (use the same scales and labels).
RPE_LABELS = {
    1: "Nothing at all",
    2: "Very easy",
    3: "Easy",
    4: "Comfortable",
    5: "Slightly challenging",
    6: "Difficult",
    7: "Hard",
    8: "Very hard",
    9: "Extremely hard",
    10: "Max effort",
}
FEEL_LABELS = {1: "Strong", 2: "Good", 3: "Normal", 4: "Poor", 5: "Weak"}
FIELDS = ("rpe", "feel")
# A start within this many seconds counts as the same session when no id links them.
MATCH_SECONDS = 120

_cache: dict[str, list[dict[str, Any]]] = {}
_CACHE_MAX = 48


class EffortError(ValueError):
    """The value was not saved."""


# Scales ---------------------------------------------------------------------------------


def garmin_rpe(value: Any) -> int | None:
    """Garmin's 10-100 effort as RPE 1-10."""
    number = _number(value)
    if number is None or number <= 0:
        return None
    return max(1, min(10, round(number / 10)))


def garmin_feel(value: Any) -> int | None:
    """Garmin's 0-100 feel (100 = very strong) as intervals.icu Feel (1 = Strong, 5 = Weak)."""
    number = _number(value)
    if number is None or number < 0:
        return None
    return max(1, min(5, round(5 - number / 25)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate(field: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EffortError(f"{field} must be a whole number")
    top = 10 if field == "rpe" else 5
    if not 1 <= value <= top:
        raise EffortError(f"{field} runs from 1 to {top}")
    return value


# intervals.icu activities from the raw archive -------------------------------------------


def _icu_month(raw, row) -> list[dict[str, Any]]:
    found = _cache.get(row.sha256)
    if found is not None:
        return found
    try:
        payload = raw.load(row.id).json()
    except Exception:
        log.warning("intervals.icu activities %s could not be read", row.item_key)
        payload = []
    out = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        start = _parse_utc(item.get("start_date"))
        out.append(
            {
                "id": str(item["id"]),
                "external_id": str(item.get("external_id") or "") or None,
                "strava_id": str(item.get("strava_id") or "") or None,
                "start": start,
                # How the session went against the plan workout intervals.icu paired it with.
                "paired_event_id": str(item.get("paired_event_id") or "") or None,
                "compliance": _number(item.get("compliance")),
                "load": _number(item.get("icu_training_load")),
                "intensity": _number(item.get("icu_intensity")),
                # Time per zone as intervals.icu counts it from its own sport settings.
                "zones": _icu_zones(item),
            }
        )
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[row.sha256] = out
    return out


def _icu_zones(item: dict[str, Any]) -> dict[str, dict[int, float]]:
    """``{"hr"|"power"|"pace": {zone number: seconds}}`` from an intervals.icu activity.
    Power comes as ``[{"id": "Z1", "secs": 600}, ...]`` (its sweet-spot band ``SS``
    overlaps two zones and is left out); heart rate and pace as a list of seconds."""
    out: dict[str, dict[int, float]] = {}
    for kind, key in (("hr", "icu_hr_zone_times"), ("pace", "pace_zone_times")):
        values = item.get(key)
        if isinstance(values, list):
            zones = {
                i + 1: float(v)
                for i, v in enumerate(values)
                if isinstance(v, int | float) and not isinstance(v, bool) and v > 0
            }
            if zones:
                out[kind] = zones
    power = item.get("icu_zone_times")
    if isinstance(power, list):
        zones = {}
        for entry in power:
            if not isinstance(entry, dict):
                continue
            ident, secs = str(entry.get("id") or ""), entry.get("secs")
            numeric = isinstance(secs, int | float) and not isinstance(secs, bool)
            if ident[:1] == "Z" and ident[1:].isdigit() and numeric and secs > 0:
                zones[int(ident[1:])] = float(secs)
        if zones:
            out["power"] = zones
    return out


def _icu_activities(engine: Engine, raw, days: set[date]) -> list[dict[str, Any]]:
    if raw is None or not days:
        return []
    months = sorted({d.strftime("%Y-%m") for d in days} | {_shift(d) for d in days})
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.id, raw_item.c.sha256, raw_item.c.item_key).where(
                raw_item.c.source == ICU,
                raw_item.c.dataset == "activities",
                raw_item.c.is_current,
                raw_item.c.item_key.in_(months),
            )
        ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.extend(_icu_month(raw, row))
    return out


def _shift(day: date) -> str:
    # A session late on the last day of a month can sit in the next month's payload.
    return (day + timedelta(days=1)).strftime("%Y-%m")


def _parse_utc(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        from datetime import UTC

        when = when.replace(tzinfo=UTC)
    return when


def _match_icu(group: dict[str, Any], icu: list[dict[str, Any]]) -> dict[str, Any] | None:
    ids = {key.split(":", 1)[1] for key in group["sources"] if key.startswith("garmin:")}
    stravas = {key.split(":", 1)[1] for key in group["sources"] if key.startswith("strava:")}
    start = _as_utc(group.get("start_utc"))
    candidates = [
        item
        for item in icu
        if (item["external_id"] and item["external_id"] in ids)
        or (item["strava_id"] and item["strava_id"] in stravas)
        or (
            start is not None
            and item["start"] is not None
            and abs((item["start"] - start).total_seconds()) <= MATCH_SECONDS
        )
    ]
    if not candidates:
        return None
    if start is None:
        return candidates[0]
    return min(
        candidates,
        key=lambda item: abs((item["start"] - start).total_seconds()) if item["start"] else 1e9,
    )


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            from datetime import UTC

            return value.replace(tzinfo=UTC)
        return value
    return _parse_utc(value)


# Reading ---------------------------------------------------------------------------------


def _garmin_values(engine: Engine, keys: list[str]) -> dict[str, dict[str, int | None]]:
    ids = [key.split(":", 1)[1] for key in keys if key.startswith("garmin:")]
    if not ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(activity.c.source_id, activity.c.extra).where(
                activity.c.source == GARMIN, activity.c.source_id.in_(ids)
            )
        ).all()
    out = {}
    for source_id, extra in rows:
        extra = extra or {}
        out[f"garmin:{source_id}"] = {
            "rpe": garmin_rpe(extra.get("rpe")),
            "feel": garmin_feel(extra.get("feel")),
        }
    return out


def _own_rows(engine: Engine, keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(activity_effort).where(activity_effort.c.source_ref.in_(keys))
        ).mappings()
        return {row["source_ref"]: dict(row) for row in rows}


def for_groups(engine: Engine, raw, groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """RPE and Feel for each unified activity (``group_id``, ``sources``, ``start_utc``,
    ``local_date``), keyed by group id."""
    if not groups:
        return {}
    keys = [key for group in groups for key in group.get("sources") or []]
    garmin = _garmin_values(engine, keys)
    own = _own_rows(engine, keys)
    return {group["group_id"]: _effort(group, garmin, own) for group in groups}


def icu_for_groups(engine: Engine, raw, groups: list[dict[str, Any]]) -> dict[str, dict]:
    """The intervals.icu activity of each unified activity, keyed by group id (when found)."""
    days = {_as_date(group.get("local_date")) for group in groups} - {None}
    icu = _icu_activities(engine, raw, days)  # type: ignore[arg-type]
    out = {}
    for group in groups:
        if found := _match_icu(group, icu):
            out[group["group_id"]] = found
    return out


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _effort(group, garmin, own) -> dict[str, Any]:
    sources = group.get("sources") or []
    garmin_row = next((garmin[key] for key in sources if key in garmin), None)
    own_row = next((own[key] for key in sources if key in own), None)
    out: dict[str, Any] = {}
    for field in FIELDS:
        values = []
        if own_row and own_row.get(field) is not None:
            values.append({"source": VIBESYNERGY, "value": own_row[field]})
        if garmin_row and garmin_row[field] is not None:
            values.append({"source": GARMIN, "value": garmin_row[field]})
        # Your own entry wins; else what the watch recorded.
        shown = values[0] if values else None
        out[field] = {
            "value": shown["value"] if shown else None,
            "source": shown["source"] if shown else None,
            "label": _label(field, shown["value"]) if shown else None,
            "editable": True,
            "others": [v for v in values if shown and v is not shown],
        }
    return out


def _label(field: str, value: int | None) -> str | None:
    if value is None:
        return None
    return (RPE_LABELS if field == "rpe" else FEEL_LABELS).get(value)


def compact(full: dict[str, Any]) -> dict[str, Any]:
    """The short form for lists: value and source per field."""
    out: dict[str, Any] = {}
    for field in FIELDS:
        out[field] = full[field]["value"]
        out[f"{field}_source"] = full[field]["source"]
    return out


def attach_compact(engine: Engine, raw, rows: list[dict[str, Any]]) -> None:
    """Add ``effort`` (compact) to activity list rows in place."""
    found = for_groups(engine, raw, rows)
    for row in rows:
        if row.get("group_id") in found:
            row["effort"] = compact(found[row["group_id"]])


def attach_full(engine: Engine, raw, rows: list[dict[str, Any]]) -> None:
    """Add ``effort`` (full, with labels and what can be entered) to rows in place."""
    found = for_groups(engine, raw, rows)
    for row in rows:
        if row.get("group_id") in found:
            row["effort"] = found[row["group_id"]]


def for_activity(engine: Engine, raw, activity_id: str) -> dict[str, Any]:
    from vibesynergy.query import views

    group = _group(engine, views.resolve_activity(engine, activity_id))
    return for_groups(engine, raw, [group])[group["group_id"]]


def _group(engine: Engine, group_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    activity_unified.c.group_id,
                    activity_unified.c.sources,
                    activity_unified.c.start_utc,
                    activity_unified.c.local_date,
                ).where(activity_unified.c.group_id == group_id)
            )
            .mappings()
            .first()
        )
    if row is None:
        raise LookupError(group_id)
    return dict(row)


# Writing ---------------------------------------------------------------------------------


def set_effort(
    engine: Engine,
    raw,
    activity_id: str,
    *,
    rpe: int | None = None,
    feel: int | None = None,
    fields: set[str] | None = None,
) -> dict[str, Any]:
    """Save RPE and/or Feel for one activity; your entry wins over Garmin's.

    ``fields`` names which of rpe/feel to change (default: the ones passed as not None), so
    ``None`` in a named field clears VibeSynergy's entry (Garmin's shows again).
    """
    from vibesynergy.query import views

    passed = (("rpe", rpe), ("feel", feel))
    chosen = fields if fields is not None else {f for f, v in passed if v is not None}
    chosen = {f for f in chosen if f in FIELDS}
    if not chosen:
        raise EffortError("nothing to save: pass rpe and/or feel")
    values = {"rpe": validate("rpe", rpe), "feel": validate("feel", feel)}
    group = _group(engine, views.resolve_activity(engine, activity_id))
    # Keep one row per activity: reuse the member the entry was first saved under.
    saved = _own_rows(engine, group["sources"])
    key = next(iter(saved), group["sources"][0])
    now = utc_now()
    update = {field: values[field] for field in chosen}
    with engine.begin() as conn:
        conn.execute(
            insert(activity_effort)
            .values(
                source_ref=key,
                subject_id=SUBJECT,
                rpe=update.get("rpe"),
                feel=update.get("feel"),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["source_ref"], set_={**update, "updated_at": now}
            )
        )
    return for_groups(engine, raw, [group])[group["group_id"]]
