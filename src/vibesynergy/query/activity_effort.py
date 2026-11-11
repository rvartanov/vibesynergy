"""RPE and Feel per activity: read from Garmin and intervals.icu, entered in VibeSynergy.

Scales follow intervals.icu, which Garmin also feeds:

- RPE 1-10 (``icu_rpe``). Garmin stores ``directWorkoutRpe`` as 10-100, so 30 is RPE 3.
- Feel 1-5 (``feel``), where 1 is Strong and 5 is Weak. Garmin stores ``directWorkoutFeel``
  as 0-100 the other way round (100 very strong, 0 very weak), so Feel = 5 - garmin / 25,
  clamped to 1-5. Checked against the athlete's activities that Garmin copied to intervals.icu.

A value recorded on a platform is shown with its source and never replaced. VibeSynergy only
keeps a value when no platform has one, then copies it to intervals.icu
(``PUT /api/v1/activity/{id}`` with ``icu_rpe`` and ``feel`` only). Before writing, the
activity is read again: a field that intervals.icu already holds with another value is left
alone and reported as a conflict. Garmin is not written: the ``garminconnect`` client has no
call for these fields.

intervals.icu activities are not normalised into VibeSynergy's activity tables; they stay in
the raw archive (one payload per month), which is parsed here and cached by content hash.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import (
    activity,
    activity_effort,
    activity_link,
    activity_unified,
    audit_log,
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
ICU_FIELD = {"rpe": "icu_rpe", "feel": "feel"}
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


def _icu_value(field: str, value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    number = round(number)
    top = 10 if field == "rpe" else 5
    return number if 1 <= number <= top else None


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
                "rpe": _icu_value("rpe", item.get("icu_rpe")),
                "feel": _icu_value("feel", item.get("feel")),
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
    days = {_as_date(group.get("local_date")) for group in groups} - {None}
    icu = _icu_activities(engine, raw, days)  # type: ignore[arg-type]
    return {group["group_id"]: _effort(group, garmin, own, icu) for group in groups}


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


def _effort(group, garmin, own, icu) -> dict[str, Any]:
    sources = group.get("sources") or []
    garmin_row = next((garmin[key] for key in sources if key in garmin), None)
    own_row = next((own[key] for key in sources if key in own), None)
    icu_row = _match_icu(group, icu)
    out: dict[str, Any] = {}
    for field in FIELDS:
        values = []
        if garmin_row and garmin_row[field] is not None:
            values.append({"source": GARMIN, "value": garmin_row[field]})
        if own_row and own_row.get(field) is not None:
            values.append({"source": VIBESYNERGY, "value": own_row[field]})
        if icu_row and icu_row[field] is not None:
            values.append({"source": ICU, "value": icu_row[field]})
        shown = _choose(values)
        out[field] = {
            "value": shown["value"] if shown else None,
            "source": shown["source"] if shown else None,
            "label": _label(field, shown["value"]) if shown else None,
            # Entering is offered only while no platform has a value of its own.
            "editable": not any(
                v["source"] != VIBESYNERGY and not _echo(v, values) for v in values
            ),
            "others": [v for v in values if shown and v is not shown],
        }
    out["intervals_icu"] = _icu_status(own_row, icu_row)
    return out


def _choose(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    # Garmin is what the watch recorded. A VibeSynergy entry wins over intervals.icu, which
    # usually just holds the copy VibeSynergy sent.
    order = {GARMIN: 0, VIBESYNERGY: 1, ICU: 2}
    return min(values, key=lambda v: order[v["source"]]) if values else None


def _echo(value: dict[str, Any], values: list[dict[str, Any]]) -> bool:
    """An intervals.icu value equal to the VibeSynergy entry is the copy VibeSynergy sent."""
    if value["source"] != ICU:
        return False
    return any(v["source"] == VIBESYNERGY and v["value"] == value["value"] for v in values)


def _label(field: str, value: int | None) -> str | None:
    if value is None:
        return None
    return (RPE_LABELS if field == "rpe" else FEEL_LABELS).get(value)


def _icu_status(own_row, icu_row) -> dict[str, Any] | None:
    if own_row is None or (own_row.get("rpe") is None and own_row.get("feel") is None):
        return None
    if not own_row.get("icu_pushed_at") and not _configured():
        return None  # intervals.icu is not connected: nothing is copied
    error = own_row.get("icu_error")
    if error == "pending":
        state = "pending"
    elif error and error.startswith("conflict"):
        state = "conflict"
    elif error == "not_found":
        state = "waiting"
    elif error:
        state = "failed"
    elif own_row.get("icu_pushed_at"):
        state = "sent"
    else:
        state = "pending"
    return {"state": state, "activity_id": own_row.get("icu_activity_id")}


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
    push: bool = True,
) -> dict[str, Any]:
    """Save RPE and/or Feel for one activity, then copy it to intervals.icu in the background.

    ``fields`` names which of rpe/feel to change (default: the ones passed as not None), so
    ``None`` in a named field clears VibeSynergy's entry. A field a platform already holds is
    refused: VibeSynergy shows that value instead of replacing it.
    """
    from vibesynergy.query import views

    passed = (("rpe", rpe), ("feel", feel))
    chosen = fields if fields is not None else {f for f, v in passed if v is not None}
    chosen = {f for f in chosen if f in FIELDS}
    if not chosen:
        raise EffortError("nothing to save: pass rpe and/or feel")
    values = {"rpe": validate("rpe", rpe), "feel": validate("feel", feel)}
    group = _group(engine, views.resolve_activity(engine, activity_id))
    current = for_groups(engine, raw, [group])[group["group_id"]]
    for field in chosen:
        if not current[field]["editable"]:
            source = current[field]["source"]
            raise EffortError(f"{field} is already recorded on {source}; it is not replaced")
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
                icu_error="pending",
            )
            .on_conflict_do_update(
                index_elements=["source_ref"],
                set_={**update, "updated_at": now, "icu_error": "pending"},
            )
        )
    if push:
        schedule_push(engine, raw, key)
    return for_groups(engine, raw, [group])[group["group_id"]]


# intervals.icu push ----------------------------------------------------------------------


def _configured() -> bool:
    from vibesynergy.sources.intervals_icu.client import account_label

    return account_label() is not None


def schedule_push(engine: Engine, raw, source_ref: str) -> None:
    if not _configured():
        log.info("RPE/Feel saved; intervals.icu not set up, nothing sent")
        return
    threading.Thread(
        target=lambda: _push_quietly(engine, raw, source_ref),
        name="icu-activity-effort",
        daemon=True,
    ).start()


def _push_quietly(engine: Engine, raw, source_ref: str, client=None) -> bool:
    try:
        return push(engine, raw, source_ref, client)
    except Exception as exc:
        log.warning("RPE/Feel saved; intervals.icu copy failed: %s", type(exc).__name__)
        _mark(engine, source_ref, error=f"{type(exc).__name__}: {exc}"[:300])
        return False


def push(engine: Engine, raw, source_ref: str, client=None) -> bool:
    """Copy VibeSynergy's RPE/Feel for one activity to intervals.icu. True when something was sent.

    Reads the activity first and sends only fields that are empty there, or still hold what
    VibeSynergy sent last time. Any other value on intervals.icu is kept and reported as a
    conflict.
    """
    from vibesynergy.mcp.identity import current_author
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    own = _own_rows(engine, [source_ref]).get(source_ref)
    if own is None:
        return False
    with engine.connect() as conn:
        group_id = conn.execute(
            select(activity_link.c.group_id).where(
                activity_link.c.source == source_ref.split(":", 1)[0],
                activity_link.c.source_id == source_ref.split(":", 1)[1],
            )
        ).scalar()
    if group_id is None:
        _mark(engine, source_ref, error="not_found")
        return False
    group = _group(engine, group_id)
    match = _match_icu(group, _icu_activities(engine, raw, {_as_date(group["local_date"])}))
    if match is None:
        _mark(engine, source_ref, error="not_found")
        return False
    client = client or IntervalsIcuClient.from_env()
    remote = client.get_activity(match["id"])
    sent_before = own.get("icu_sent") or {}
    body: dict[str, int] = {}
    conflicts = []
    before: dict[str, Any] = {}
    for field in FIELDS:
        mine = own.get(field)
        if mine is None:
            continue
        name = ICU_FIELD[field]
        there = _icu_value(field, remote.get(name))
        if there == mine:
            continue
        if there is None or there == sent_before.get(name):
            body[name] = mine
            before[name] = remote.get(name)
        else:
            conflicts.append(field)
    if body:
        client.update_activity(match["id"], body)
        with engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(),
                    actor="vibesynergy",
                    action="icu.activity_effort",
                    detail={
                        "activity": source_ref,
                        "icu_activity_id": match["id"],
                        "before": before,
                        "after": body,
                    },
                    author=current_author(),
                    scope="diary.write",
                    target=match["id"][:200],
                    outcome="ok",
                )
            )
    _mark(
        engine,
        source_ref,
        error=("conflict:" + ",".join(conflicts)) if conflicts else None,
        activity_id=match["id"],
        sent={**sent_before, **body},
        pushed=True,
    )
    return bool(body)


def retry_pending(engine: Engine, raw, client=None) -> int:
    """After an intervals.icu sync: send entries that are pending, failed or waited for the
    activity to appear there. Conflicts stay until the value is changed again."""
    with engine.connect() as conn:
        refs = [
            row.source_ref
            for row in conn.execute(
                select(activity_effort.c.source_ref, activity_effort.c.icu_error).where(
                    activity_effort.c.icu_error.is_not(None)
                )
            )
            if not str(row.icu_error).startswith("conflict")
        ]
    if not refs or (client is None and not _configured()):
        return 0
    sent = 0
    for ref in refs:
        if _push_quietly(engine, raw, ref, client):
            sent += 1
    return sent


def _mark(
    engine: Engine,
    source_ref: str,
    *,
    error: str | None,
    activity_id: str | None = None,
    sent: dict | None = None,
    pushed: bool = False,
) -> None:
    values: dict[str, Any] = {"icu_error": error}
    if activity_id:
        values["icu_activity_id"] = activity_id
    if sent is not None:
        values["icu_sent"] = sent
    if pushed:
        values["icu_pushed_at"] = utc_now()
    with engine.begin() as conn:
        conn.execute(
            activity_effort.update()
            .where(activity_effort.c.source_ref == source_ref)
            .values(**values)
        )
