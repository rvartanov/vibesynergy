"""AI coach writes to the intervals.icu plan, and the Garmin Coach duplicate link.

Writes go to one event id. An ``external_id`` only finds that id in the cache.
Nothing bulk-upserts. Every write stores a before and after snapshot on
``audit_log`` and a line on the coach feed. Revert puts the before snapshot
back through the API.

While intervals.icu leads, its calendar is the plan (``plan_layer``; merged in
``query/plan.py``). When VibeSynergy leads, the plan is its own model and these events are
its copy. A Garmin calendar workout whose name and date match a plan workout is the copy
intervals.icu pushed and is folded into that workout. A real Garmin Coach workout on the
same date and sport as a plan workout is not in the plan; it is linked as one pair with
``flag`` ``dubbel``. Neither side is deleted until ``resolve_duplicate`` says so.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.identity import current_author, current_connection
from vibesynergy.model import IcuEvent
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient, IntervalsIcuError
from vibesynergy.sources.intervals_icu.mappers import TARGET_KEYS, _updated, map_events
from vibesynergy.store.raw import RawRecord
from vibesynergy.store.schema import audit_log, coach_decision_log, icu_event, raw_item

Keep = Literal["garmin", "plan"]
# A value from before the rename, still accepted on input: "coach" meant Garmin Coach.
_LEGACY_KEEP: dict[str, Keep] = {"coach": "garmin"}
# Actor on the revertable before/after snapshot rows. The MCP call itself is a separate
# ``actor="mcp"`` row; ``author`` and ``client_id`` say which coach app made the change.
PLAN_ACTOR = "plan"


class IcuWriteError(ValueError):
    """The plan was not changed."""


def version_token(updated: datetime | None, raw: dict | None = None) -> str:
    """The cached version the coach must send back before a write."""
    if updated is not None:
        return updated.astimezone(UTC).isoformat(timespec="seconds")
    digest = json.dumps(raw or {}, sort_keys=True, default=str).encode()
    return "raw:" + hashlib.sha256(digest).hexdigest()[:16]


def versions_match(expected: str, actual: str) -> bool:
    text = (expected or "").strip()
    if text == actual:
        return True
    parsed = _updated(text)
    return parsed is not None and version_token(parsed, None) == actual


PLAN_CATEGORIES = ("WORKOUT", "NOTE", "TARGET")
RACE_PRIORITY = {"RACE_A": "A", "RACE_B": "B", "RACE_C": "C"}


def plan_layer(engine: Engine, start: date, end: date) -> dict[str, Any]:
    """The intervals.icu events that make the plan between ``start`` and ``end``, while
    intervals.icu leads (``plan_model.plan_layer`` is its twin for VibeSynergy's own plan).

    ``workouts`` are WORKOUT events in the window. ``notes`` and ``targets`` are the weekly
    NOTE and TARGET events from the Monday of ``start`` on. ``plan_start`` is the first day
    of any plan workout (one with an external_id), across the whole cache: from that day on
    the intervals.icu calendar is the plan. ``marks`` are the Garmin Coach sessions the
    coach marked for the athlete.
    """
    monday = start - timedelta(days=start.weekday())
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                select(icu_event)
                .where(
                    icu_event.c.category.in_(PLAN_CATEGORIES),
                    icu_event.c.date >= monday,
                    icu_event.c.date <= end,
                )
                .order_by(icu_event.c.date, icu_event.c.id)
            ).mappings()
        ]
        plan_start = conn.execute(
            select(func.min(icu_event.c.date)).where(
                icu_event.c.category == "WORKOUT", icu_event.c.source == "plan"
            )
        ).scalar()
        marks = _athlete_marks(conn) if rows else {}
    return {
        "workouts": [r for r in rows if r["category"] == "WORKOUT" and r["date"] >= start],
        "notes": [r for r in rows if r["category"] == "NOTE" and r["for_week"]],
        "targets": [r for r in rows if r["category"] == "TARGET"],
        "plan_start": plan_start,
        "marks": marks,
    }


def races(engine: Engine, since: date) -> list[dict[str, Any]]:
    """RACE_A/B/C events from ``since`` on, shaped like a ``planned_item`` event row."""
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                select(icu_event)
                .where(icu_event.c.category.in_(tuple(RACE_PRIORITY)), icu_event.c.date >= since)
                .order_by(icu_event.c.date)
            ).mappings()
        ]
    out = []
    for row in rows:
        raw = row.get("raw") or {}
        distance = raw.get("distance")
        out.append(
            {
                "source": "intervals_icu",
                "source_id": row["id"],
                "date": row["date"],
                "kind": "event",
                "title": row.get("name"),
                "sport": row.get("type"),
                "plan_id": None,
                "is_race": True,
                "primary": False,
                "extra": {
                    "icu_id": row["id"],
                    "external_id": row.get("external_id"),
                    "category": row["category"],
                    "icu_priority": RACE_PRIORITY[row["category"]],
                    "description": row.get("description"),
                    **(
                        {"distance": float(distance)}
                        if isinstance(distance, (int, float)) and distance >= 100
                        else {}
                    ),
                    "ownership": "intervals_plan" if row["source"] == "plan" else "intervals",
                },
            }
        )
    return out


def season(engine: Engine, day: date) -> dict[str, Any] | None:
    """The intervals.icu season (latest SEASON_START on or before ``day``), if any."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(icu_event.c.id, icu_event.c.date, icu_event.c.name, icu_event.c.description)
                .where(icu_event.c.category == "SEASON_START", icu_event.c.date <= day)
                .order_by(icu_event.c.date.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "start": str(row["date"]),
        "name": row["name"],
        "text": row["description"],
    }


def create_event(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    reason: str,
    category: str,
    start_date: date,
    name: str,
    sport: str | None = None,
    description: str | None = None,
    external_id: str | None = None,
    end_date: date | None = None,
    for_week: bool = False,
    tags: list[str] | None = None,
    targets: dict[str, float] | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    if not category.strip() or not name.strip():
        raise IcuWriteError("A plan session needs a name and a kind.")
    _check_block(description)
    external = (external_id or "").strip() or None
    with engine.connect() as conn:
        if external and _by_external(conn, external) is not None:
            raise IcuWriteError(
                "That session key is already on the plan. Change it by its event id."
            )
        raw_id = _calendar_raw_id(conn)
    body: dict[str, Any] = {
        "category": category.strip(),
        "name": name.strip(),
        "start_date_local": f"{start_date.isoformat()}T00:00:00",
        "for_week": bool(for_week),
    }
    if sport:
        body["type"] = sport
    if description:
        body["description"] = description
    if external:
        body["external_id"] = external
    if end_date:
        body["end_date_local"] = f"{end_date.isoformat()}T00:00:00"
    if tags:
        body["tags"] = tags
    for key, value in (targets or {}).items():
        if key not in TARGET_KEYS:
            raise IcuWriteError(f"Unknown target {key!r}; use {', '.join(TARGET_KEYS)}.")
        if not isinstance(value, int | float) or value <= 0:
            raise IcuWriteError(f"Target {key} must be a positive number.")
        body[key] = value
    created = _object(client.create_event(body))
    stored = {**body, **created}
    return _finish(
        engine,
        action="icu.create",
        reason=reason,
        before=None,
        after=stored,
        event_id=str(stored["id"]),
        raw_id=raw_id,
        author=author or current_author(),
        day=start_date,
    )


def update_event(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    reason: str,
    expected_version: str,
    changes: dict[str, Any],
    event_id: str | None = None,
    external_id: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    _check_block(changes.get("description"))
    with engine.connect() as conn:
        row = _resolve(conn, event_id, external_id)
        raw_id = _calendar_raw_id(conn)
    _require_current(client, row, expected_version)
    body = dict(row["raw"] or {})
    body.pop("id", None)
    for key, value in changes.items():
        if value is not None:
            body[key] = value
    updated = _merged(client.update_event(row["id"], body), row["id"], body)
    stored = updated
    return _finish(
        engine,
        action="icu.update",
        reason=reason,
        before=row["raw"],
        after=stored,
        event_id=str(stored["id"]),
        raw_id=raw_id,
        author=author or current_author(),
        day=row["date"],
    )


def move_event(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    reason: str,
    expected_version: str,
    start_date: date,
    event_id: str | None = None,
    external_id: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    with engine.connect() as conn:
        row = _resolve(conn, event_id, external_id)
        raw_id = _calendar_raw_id(conn)
    _require_current(client, row, expected_version)
    body = dict(row["raw"] or {})
    old = _day(body.get("start_date_local"), row["date"])
    delta = start_date - old
    body["start_date_local"] = _shift(body.get("start_date_local"), delta, start_date)
    if body.get("end_date_local"):
        body["end_date_local"] = _shift(body.get("end_date_local"), delta, None)
    body.pop("id", None)
    updated = _merged(client.update_event(row["id"], body), row["id"], body)
    stored = updated
    return _finish(
        engine,
        action="icu.move",
        reason=reason,
        before=row["raw"],
        after=stored,
        event_id=str(stored["id"]),
        raw_id=raw_id,
        author=author or current_author(),
        day=start_date,
    )


def delete_event(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    reason: str,
    expected_version: str,
    event_id: str | None = None,
    external_id: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    with engine.connect() as conn:
        row = _resolve(conn, event_id, external_id)
        raw_id = _calendar_raw_id(conn)
    _require_current(client, row, expected_version)
    client.delete_event(row["id"])
    return _finish(
        engine,
        action="icu.delete",
        reason=reason,
        before=row["raw"],
        after=None,
        event_id=row["id"],
        raw_id=raw_id,
        author=author or current_author(),
        day=row["date"],
        remove=True,
    )


def revert(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    audit_id: int,
    reason: str,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    with engine.connect() as conn:
        found = conn.execute(select(audit_log).where(audit_log.c.id == audit_id)).mappings().first()
    if found is None:
        raise IcuWriteError("That change is not in the audit log.")
    detail = found["detail"] or {}
    if not detail.get("revertable"):
        raise IcuWriteError("That audit entry cannot be reverted.")
    action = detail.get("action")
    before = detail.get("before")
    after = detail.get("after")
    who = author or current_author()
    if action == "icu.wellness":
        raise IcuWriteError(
            "Check-ins are no longer copied to intervals.icu, so that copy can't be undone here."
        )
    if action == "icu.mark":
        return _clear_mark(engine, detail, reason, who, audit_id)
    with engine.connect() as conn:
        raw_id = _calendar_raw_id(conn)
    if before is None and isinstance(after, dict):
        event_id = str(after.get("id") or detail.get("event_id"))
        _require_live(client, event_id, after)
        client.delete_event(event_id)
        return _finish(
            engine,
            action="icu.revert",
            reason=reason,
            before=after,
            after=None,
            event_id=event_id,
            raw_id=raw_id,
            author=who,
            day=_day((after or {}).get("start_date_local"), date.today()),
            remove=True,
            extra={"reverts": audit_id},
        )
    if after is None and isinstance(before, dict):
        if _still_there(client, str(before.get("id") or "")):
            raise IcuWriteError("That session is already on the plan. Not adding it again.")
        body = {key: value for key, value in before.items() if key != "id"}
        created = _object(client.create_event(body))
        stored = {**body, **created}
        return _finish(
            engine,
            action="icu.revert",
            reason=reason,
            before=None,
            after=stored,
            event_id=str(stored["id"]),
            raw_id=raw_id,
            author=who,
            day=_day(stored.get("start_date_local"), date.today()),
            extra={"reverts": audit_id},
        )
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise IcuWriteError("That audit entry has no snapshot to restore.")
    event_id = str(before.get("id") or after.get("id") or detail.get("event_id"))
    _require_live(client, event_id, after)
    body = {key: value for key, value in before.items() if key != "id"}
    stored = _merged(client.update_event(event_id, body), event_id, body)
    return _finish(
        engine,
        action="icu.revert",
        reason=reason,
        before=after,
        after=stored,
        event_id=str(stored["id"]),
        raw_id=raw_id,
        author=who,
        day=_day(stored.get("start_date_local"), date.today()),
        extra={"reverts": audit_id},
    )


def resolve_duplicate(
    engine: Engine,
    client: IntervalsIcuClient,
    *,
    keep: Keep | str,
    reason: str,
    expected_version: str,
    event_id: str | None = None,
    external_id: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    _reason(reason)
    keep = _LEGACY_KEEP.get(keep, keep)
    if keep not in {"garmin", "plan"}:
        raise IcuWriteError("keep must be garmin or plan.")
    with engine.connect() as conn:
        row = _resolve(conn, event_id, external_id)
    if keep == "garmin":
        deleted = delete_event(
            engine,
            client,
            reason=reason,
            expected_version=expected_version,
            event_id=row["id"],
            author=author,
        )
        deleted["kept"] = "garmin"
        return deleted
    _require_current(client, row, expected_version)
    coach = _coach_partner(engine, row)
    who = author or current_author()
    with engine.begin() as conn:
        audit_id = _audit(
            conn,
            action="icu.mark",
            reason=reason,
            before=row["raw"],
            after=row["raw"],
            event_id=row["id"],
            author=who,
            extra={"mark": "athlete", "garmin_source_id": coach["source_id"]},
        )
        _feed(
            conn,
            reason=reason,
            day=row["date"],
            author=who,
            meta={
                "audit_id": audit_id,
                "action": "icu.mark",
                "event_id": row["id"],
                "garmin_source_id": coach["source_id"],
                "mark": "athlete",
            },
        )
    return {
        "kept": "plan",
        "event_id": row["id"],
        "version": version_token(row["updated"], row["raw"]),
        "audit_id": audit_id,
        "marked_for_athlete": True,
        "garmin_source_id": coach["source_id"],
    }


def link_duplicate(item: dict, row: dict[str, Any], marks: dict[str, bool]) -> None:
    """Flag a Garmin Coach session and a plan workout on one day and sport as ``dubbel``."""
    item["flag"] = "dubbel"
    item["duplicate"] = {
        "garmin": {
            "source": "garmin",
            "source_id": item.get("source_id"),
            "title": item.get("title"),
        },
        "plan": {
            "id": row["id"],
            "external_id": row.get("external_id"),
            "name": row.get("name"),
            "version": version_token(row.get("updated"), row.get("raw") or {}),
        },
        "marked_for_athlete": bool(marks.get(str(item.get("source_id")))),
    }


def _athlete_marks(conn) -> dict[str, bool]:
    rows = conn.execute(
        select(coach_decision_log.c.meta)
        .where(coach_decision_log.c.entry_type == "plan_change")
        .order_by(coach_decision_log.c.id.desc())
        .limit(200)
    ).scalars()
    seen: dict[str, bool] = {}
    for meta in rows:
        if not isinstance(meta, dict):
            continue
        garmin_id = meta.get("garmin_source_id")
        if not garmin_id or garmin_id in seen:
            continue
        seen[str(garmin_id)] = meta.get("mark") == "athlete"
    return {key: True for key, on in seen.items() if on}


def _coach_partner(engine: Engine, row: dict) -> dict:
    from vibesynergy.query.plan import family
    from vibesynergy.store.schema import planned_item

    sport = family(row.get("type"))
    if not sport:
        raise IcuWriteError("This plan session has no sport to match.")
    with engine.connect() as conn:
        candidates = [
            dict(item)
            for item in conn.execute(
                select(planned_item).where(
                    planned_item.c.source == "garmin",
                    planned_item.c.kind == "plan_workout",
                    planned_item.c.date == row["date"],
                )
            ).mappings()
        ]
    matches = [item for item in candidates if family(item.get("sport")) == sport]
    if len(matches) != 1:
        raise IcuWriteError(
            "There is not exactly one Garmin Coach session that day for that sport."
        )
    return matches[0]


def _clear_mark(engine: Engine, detail: dict, reason: str, author: str, audit_id: int) -> dict:
    garmin_id = detail.get("garmin_source_id")
    with engine.begin() as conn:
        new_id = _audit(
            conn,
            action="icu.revert",
            reason=reason,
            before=detail.get("before"),
            after=detail.get("after"),
            event_id=str(detail.get("event_id") or ""),
            author=author,
            extra={"reverts": audit_id, "mark": None, "garmin_source_id": garmin_id},
        )
        _feed(
            conn,
            reason=reason,
            day=None,
            author=author,
            meta={
                "audit_id": new_id,
                "action": "icu.revert",
                "event_id": detail.get("event_id"),
                "garmin_source_id": garmin_id,
                "mark": None,
            },
        )
    return {"kept": "cleared", "audit_id": new_id, "marked_for_athlete": False}


def _require_current(client: IntervalsIcuClient, row: dict, expected: str) -> None:
    if not (expected or "").strip():
        raise IcuWriteError("Send the version from the last read.")
    actual = version_token(row.get("updated"), row.get("raw") or {})
    if not versions_match(expected, actual):
        raise IcuWriteError(
            "This session changed since you last read it. Read it again before changing it."
        )
    live = _object(client.get_event(row["id"]))
    live_version = version_token(_updated(live.get("updated")), live)
    if live_version != actual:
        raise IcuWriteError(
            "intervals.icu has a newer copy. Read it again with vibesynergy_icu_events "
            "(refresh=true), then change it."
        )


def _require_live(client: IntervalsIcuClient, event_id: str, snapshot: dict) -> None:
    live = _object(client.get_event(event_id))
    expected = version_token(_updated(snapshot.get("updated")), snapshot)
    actual = version_token(_updated(live.get("updated")), live)
    if not versions_match(expected, actual) and expected != actual:
        raise IcuWriteError(
            "This session changed since that edit. Read it again before undoing it."
        )


def _still_there(client: IntervalsIcuClient, event_id: str) -> bool:
    if not event_id:
        return False
    try:
        client.get_event(event_id)
    except IntervalsIcuError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise
    return True


def _finish(
    engine: Engine,
    *,
    action: str,
    reason: str,
    before: dict | None,
    after: dict | None,
    event_id: str,
    raw_id: str,
    author: str,
    day: date | None,
    remove: bool = False,
    extra: dict | None = None,
) -> dict[str, Any]:
    with engine.begin() as conn:
        if remove:
            conn.execute(icu_event.delete().where(icu_event.c.id == str(event_id)))
        elif after is not None:
            _cache(conn, after, raw_id)
        audit_id = _audit(
            conn,
            action=action,
            reason=reason,
            before=before,
            after=after,
            event_id=str(event_id),
            author=author,
            extra=extra,
        )
        _feed(
            conn,
            reason=reason,
            day=day,
            author=author,
            meta={"audit_id": audit_id, "action": action, "event_id": str(event_id)},
        )
    version = None
    if isinstance(after, dict):
        version = version_token(_updated(after.get("updated")), after)
    return {
        "event_id": str(event_id),
        "external_id": _external(after, before),
        "version": version,
        "audit_id": audit_id,
        "action": action,
    }


def _cache(conn, event: dict, raw_id: str) -> None:
    fetched = utc_now()
    payload = json.dumps([event]).encode()
    raw = RawRecord(
        id=raw_id,
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        sha256="",
        content_type="application/json",
        request={},
        window_start=None,
        window_end=None,
        fetched_at=fetched,
        supersedes=None,
        payload=payload,
    )
    records = [record for record in map_events(raw) if isinstance(record, IcuEvent)]
    if len(records) != 1:
        raise IcuWriteError("The saved session could not be cached.")
    write_records(conn, records, source="intervals_icu", raw_item_id=raw_id)


def _audit(
    conn,
    *,
    action: str,
    reason: str,
    before: dict | None,
    after: dict | None,
    event_id: str,
    author: str,
    extra: dict | None = None,
) -> int:
    detail = {
        "reason": reason,
        "before": before,
        "after": after,
        "event_id": event_id,
        "action": action,
        "revertable": True,
    }
    if extra:
        detail.update(extra)
    return conn.execute(
        audit_log.insert()
        .values(
            at=utc_now(),
            actor=PLAN_ACTOR,
            action=action,
            detail=detail,
            client_id=current_connection().client,
            author=author,
            scope="plan.write",
            target=event_id[:200],
            outcome="ok",
        )
        .returning(audit_log.c.id)
    ).scalar_one()


def _feed(conn, *, reason: str, day: date | None, author: str, meta: dict) -> None:
    conn.execute(
        coach_decision_log.insert().values(
            subject_id=1,
            at=utc_now(),
            kind="note",
            author=author or "user",
            entry_type="plan_change",
            for_date=day,
            text=reason[:4000],
            meta=meta,
        )
    )


def list_events(
    engine: Engine, start: date, end: date, category: str | None = None
) -> list[dict[str, Any]]:
    """Cached intervals.icu plan events in the inclusive date window, with write handles.

    ``version`` is the token the write tools expect as ``expected_version``.
    """
    query = select(icu_event).where(icu_event.c.date >= start, icu_event.c.date <= end)
    if category:
        query = query.where(icu_event.c.category == category.strip().upper())
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(query.order_by(icu_event.c.date)).mappings()]
    out = []
    for row in rows:
        raw = row.get("raw") or {}
        out.append(
            {
                "event_id": str(row["id"]),
                "external_id": row.get("external_id"),
                "version": version_token(row.get("updated"), raw),
                "date": row["date"].isoformat(),
                "end_date": (raw.get("end_date_local") or "")[:10] or None,
                "category": row.get("category"),
                "sport": row.get("type"),
                "name": row.get("name"),
                "description": row.get("description"),
                "tags": row.get("tags") or [],
                "for_week": bool(row.get("for_week")),
                "targets": row.get("targets") or {},
                "moving_time_s": raw.get("moving_time"),
                "load": raw.get("icu_training_load"),
                "fetched_at": row["fetched_at"].isoformat() if row.get("fetched_at") else None,
            }
        )
    return out


def _resolve(conn, event_id: str | None, external_id: str | None) -> dict:
    if event_id:
        row = (
            conn.execute(select(icu_event).where(icu_event.c.id == str(event_id)))
            .mappings()
            .first()
        )
        if row is None:
            raise IcuWriteError(
                "That session is not in the cache. Read it with vibesynergy_icu_events "
                "(refresh=true) and use its event_id and version."
            )
        found = dict(row)
        external = (external_id or "").strip()
        if external and found.get("external_id") != external:
            raise IcuWriteError("That event id does not belong to that session key.")
        return found
    external = (external_id or "").strip()
    if not external:
        raise IcuWriteError("Give the event id or the session key.")
    found = _by_external(conn, external)
    if found is None:
        raise IcuWriteError(
            "That session key is not in the cache. Read it with vibesynergy_icu_events "
            "(refresh=true) and use its event_id and version."
        )
    return found


def _by_external(conn, external_id: str) -> dict | None:
    rows = [
        dict(row)
        for row in conn.execute(
            select(icu_event).where(icu_event.c.external_id == external_id)
        ).mappings()
    ]
    if len(rows) > 1:
        raise IcuWriteError("That session key matches more than one event. Use the event id.")
    return rows[0] if rows else None


def _calendar_raw_id(conn) -> str:
    found = conn.execute(
        select(raw_item.c.id).where(
            raw_item.c.source == "intervals_icu",
            raw_item.c.dataset == "events",
            raw_item.c.item_key == "calendar",
            raw_item.c.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if not found:
        raise IcuWriteError("Sync intervals.icu before changing the plan.")
    return found


def _external(after: dict | None, before: dict | None) -> str | None:
    snapshot = after if isinstance(after, dict) else before if isinstance(before, dict) else {}
    return snapshot.get("external_id")


def _object(payload: Any) -> dict:
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise IcuWriteError("intervals.icu did not return the session.")
    return payload


def _merged(payload: Any, event_id: str, body: dict) -> dict:
    if isinstance(payload, dict) and payload.get("id") is not None:
        return {**body, **payload}
    stored = dict(body)
    stored["id"] = event_id
    return stored


def _check_block(description: str | None) -> None:
    """Refuse a ``[garmin]`` exercise block that would not reach Garmin."""
    from vibesynergy.query import garmin_strength

    try:
        garmin_strength.check_description(description)
    except garmin_strength.BlockError as exc:
        raise IcuWriteError(f"The [garmin] block cannot go to Garmin: {exc}") from exc


def _reason(reason: str) -> None:
    if not (reason or "").strip():
        raise IcuWriteError("A reason is required.")


def _name_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _day(value: Any, fallback: date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return fallback
    return fallback


def _shift(value: Any, delta: timedelta, fallback: date | None) -> str:
    if not isinstance(value, str) or not value:
        day = fallback or date.today()
        return f"{day.isoformat()}T00:00:00"
    try:
        day = date.fromisoformat(value[:10]) + delta
    except ValueError:
        day = fallback or date.today()
        return f"{day.isoformat()}T00:00:00"
    if "T" in value:
        return day.isoformat() + value[10:]
    return day.isoformat()
