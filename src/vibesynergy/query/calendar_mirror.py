"""Garmin schedule → one selected CalDAV calendar.

Personal events stay user-owned: this job never creates, updates or deletes them.
Only a mirror whose legacy UID, href and ``X-HERMES-*`` proof match a Garmin native key
can change, and only after that proof is stored in the ledger. Titles and dates are not
ownership. Garmin Coach workouts are copied onto the calendar; nothing is written back
to Garmin.

The portal switch defaults off. A failed or incomplete Garmin read never authorizes a
deletion. An existing mirror is only changed on a later reconcile, after its content has
been recorded and is still the same — so a crash, or an edit made after that reading,
cannot be overwritten or removed blindly. Creates of a missing object are conditional
(``If-None-Match: *``) and are not retried until a read shows the object is still absent.

A write whose outcome was not seen leaves the ledger row ``uncertain``. The next complete
read settles it: the object shows the content that was to be written (verified), still
shows the recorded content (the conditional write is tried again against its current
ETag), or is gone (absent). Anything else stays a conflict. A delete also needs the Garmin
read to have reached the mirror's date. A ledger row from another account authorizes nothing.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.query import evidence, views
from vibesynergy.query.ownership import (
    CreateIntent,
    ExternalObjectRef,
    OwnershipClaim,
    can_write,
    create_idempotency_key,
    idempotency_key,
)
from vibesynergy.sources.caldav import account
from vibesynergy.sources.caldav.calendar import parse_object
from vibesynergy.sources.caldav.ics import content_fingerprint, href_for, render_event, uid_for
from vibesynergy.sources.caldav.writer import (
    CalendarWriter,
    MirrorTimeout,
    MirrorWriteError,
    PreconditionFailed,
)
from vibesynergy.store.schema import calendar_mirror, dataset_state, planned_item, raw_item

log = logging.getLogger(__name__)

JOB = "calendar_mirror"
MAX_ACTIONS = 500
PREVIEW_ACTIONS = 200
CREATE_LOOKAHEAD_DAYS = 183
GARMIN_STALE_AFTER = timedelta(hours=36)
_MUTABLE = frozenset({"verified", "held"})
_UNKNOWN_OUTCOME = frozenset({"uncertain", "reserved"})
_UPDATABLE = _MUTABLE | {"conflict", "uncertain"}
_DELETABLE = _MUTABLE | {"uncertain"}


@dataclass(frozen=True)
class Desired:
    origin_id: str
    day: date
    title: str | None


@dataclass(frozen=True)
class Action:
    op: str
    origin_id: str
    href: str | None = None
    uid: str | None = None
    etag: str | None = None
    sequence: int = 0
    fingerprint: str | None = None
    event_date: date | None = None
    body: bytes | None = None
    error: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class PlannedActions:
    actions: list[Action]
    unreadable_objects: int = 0


def mirror_active(settings) -> bool:
    return account.mirror_enabled(settings) and account.writes_allowed(settings)


def due(rt) -> bool:
    return mirror_active(rt.settings)


def account_id(stored: dict) -> str:
    return evidence.digest(
        {"provider": stored.get("provider") or "", "username": stored.get("username") or ""}
    )


def _ids(origin_id: str, calendar_url: str) -> tuple[str, str]:
    ledger_id = evidence.digest(
        {"role": "ledger", "origin_id": origin_id, "calendar": calendar_url}
    )
    intent_id = evidence.digest(
        {"role": "intent", "origin_id": origin_id, "calendar": calendar_url}
    )
    return ledger_id, intent_id


def _skipped(code: str) -> dict[str, Any]:
    return {
        "skipped": code,
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "adopted": 0,
        "conflicts": 0,
        "ignored_unreadable": 0,
        "batch_limited": False,
    }


def _stamp(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def _inspect(calendar) -> tuple[str, date, date, int]:
    event = calendar.walk("VEVENT")[0]
    start = event.decoded("DTSTART")
    end = event.decoded("DTEND")
    sequence = event.get("SEQUENCE")
    if type(start) is not date or type(end) is not date or not isinstance(sequence, int):
        raise ValueError("Verified mirror is missing an all-day date or sequence")
    summary = str(event.get("SUMMARY") or "")
    return content_fingerprint(summary, start, end), start, end, sequence


def plan_actions(
    *,
    desired: list[Desired],
    remote: list[dict[str, str]],
    ledger: list[dict[str, Any]],
    calendar_url: str,
    today: date,
    mutate: bool,
    deletes_allowed: bool,
    covered_until: date | None,
    account_id: str,
    stamp: str,
) -> PlannedActions:
    """Decide ledger updates and conditional writes. Personal objects produce no action.

    Ledger rows recorded for another provider account never authorize anything here: the
    mirror is adopted again under this account first. A delete needs the Garmin read to
    have covered the mirror's date, so an object past the fetched window is only held.
    An ``uncertain`` row (a write whose outcome was not seen) is resolved from what the
    complete calendar read shows now, never from a title or date guess.
    """
    mirrors: dict[str, tuple[dict[str, str], Any, str, date]] = {}
    unreadable_objects = 0
    for obj in remote:
        try:
            parsed, key = parse_object(obj, calendar_url)
        except ValueError:
            unreadable_objects += 1
            continue
        if key is None:
            continue
        if key in mirrors:
            return PlannedActions(
                [Action("abort", key, error="duplicate_mirror")],
                unreadable_objects=unreadable_objects,
            )
        fingerprint, start, _end, _sequence = _inspect(parsed)
        mirrors[key] = (obj, parsed, fingerprint, start)

    wanted: dict[str, Desired | None] = {}
    for item in desired:
        if item.origin_id in wanted:
            wanted[item.origin_id] = None
        else:
            wanted[item.origin_id] = item
    known = {row["origin_id"]: row for row in ledger if row.get("account_id") == account_id}
    actions: list[Action] = []

    # Resolve the saved write before considering the latest Garmin revision. One pass
    # only records the read-back; the next may apply a newer revision or cancellation.
    recovered = set()
    for origin, row in known.items():
        pending = row.get("pending_write") or {}
        found = mirrors.get(origin)
        if row.get("state") not in _UNKNOWN_OUTCOME or not found:
            continue
        obj, parsed, remote_fp, start = found
        sequence = _inspect(parsed)[3]
        if (
            pending.get("op") in {"create", "update"}
            and remote_fp == pending.get("fingerprint")
            and sequence == pending.get("sequence")
        ):
            actions.append(
                Action(
                    "remember",
                    origin,
                    obj["url"],
                    uid_for(origin),
                    etag=obj["etag"],
                    fingerprint=remote_fp,
                    event_date=start,
                    sequence=sequence,
                    state="verified",
                )
            )
            recovered.add(origin)

    for origin, item in wanted.items():
        if origin in recovered:
            continue
        href, uid = href_for(calendar_url, origin), uid_for(origin)
        if item is None:
            actions.append(Action("conflict", origin, href, uid, error="duplicate_garmin_key"))
            continue
        end = item.day + timedelta(days=1)
        fingerprint = content_fingerprint(item.title, item.day, end)
        found = mirrors.get(origin)
        row = known.get(origin)
        writable_day = covered_until is not None and today <= item.day <= covered_until
        if found is None:
            if mutate and writable_day:
                actions.append(
                    Action(
                        "create",
                        origin,
                        href,
                        uid,
                        sequence=0,
                        fingerprint=fingerprint,
                        event_date=item.day,
                        body=render_event(origin, item.day, item.title, sequence=0, stamp=stamp),
                    )
                )
            continue
        obj, _parsed, remote_fp, start = found
        if row is None or row.get("fingerprint") != remote_fp:
            state = (
                "conflict"
                if row is not None and row.get("fingerprint") not in (None, remote_fp)
                else "held"
            )
            error = "external_edit" if state == "conflict" else None
            # First sight stores the remote content. A later edit (fingerprint moved) conflicts
            # and keeps the previous owned fingerprint so a revert can be recognised.
            if state == "conflict":
                actions.append(
                    Action(
                        "remember",
                        origin,
                        href,
                        uid,
                        etag=obj["etag"],
                        fingerprint=row.get("fingerprint"),
                        event_date=start,
                        error=error,
                        state="conflict",
                    )
                )
            else:
                actions.append(
                    Action(
                        "remember",
                        origin,
                        href,
                        uid,
                        etag=obj["etag"],
                        fingerprint=remote_fp,
                        event_date=start,
                        state="verified" if remote_fp == fingerprint else "held",
                    )
                )
            continue
        if remote_fp == fingerprint:
            if row.get("state") != "verified" or row.get("etag") != obj["etag"]:
                actions.append(
                    Action(
                        "remember",
                        origin,
                        href,
                        uid,
                        etag=obj["etag"],
                        fingerprint=remote_fp,
                        event_date=item.day,
                        state="verified",
                    )
                )
            continue
        if not mutate or not writable_day or row.get("state") not in _UPDATABLE:
            continue
        sequence = _inspect(found[1])[3] + 1
        actions.append(
            Action(
                "update",
                origin,
                href,
                uid,
                etag=obj["etag"],
                sequence=sequence,
                fingerprint=fingerprint,
                event_date=item.day,
                body=render_event(origin, item.day, item.title, sequence=sequence, stamp=stamp),
            )
        )

    for origin, (obj, _parsed, remote_fp, start) in mirrors.items():
        if origin in wanted or origin in recovered:
            continue
        href, uid = href_for(calendar_url, origin), uid_for(origin)
        row = known.get(origin)
        may_delete = (
            deletes_allowed
            and covered_until is not None
            and start <= min(covered_until, today + timedelta(days=CREATE_LOOKAHEAD_DAYS))
        )
        if (
            row is None
            or row.get("fingerprint") != remote_fp
            or start < today
            or not may_delete
            or not mutate
            or row.get("state") not in _DELETABLE
        ):
            if row is not None and row.get("fingerprint") not in (None, remote_fp):
                if (
                    row.get("state") == "conflict"
                    and row.get("etag") == obj["etag"]
                    and row.get("last_error") == "external_edit"
                ):
                    continue
                actions.append(
                    Action(
                        "remember",
                        origin,
                        href,
                        uid,
                        etag=obj["etag"],
                        fingerprint=row.get("fingerprint"),
                        event_date=start,
                        error="external_edit",
                        state="conflict",
                    )
                )
            elif start < today or row is None or not may_delete or not mutate:
                if (
                    row is not None
                    and row.get("state") == "held"
                    and row.get("etag") == obj["etag"]
                    and row.get("fingerprint") == remote_fp
                    and row.get("event_date") == start
                    and row.get("last_error") is None
                ):
                    continue
                actions.append(
                    Action(
                        "remember",
                        origin,
                        href,
                        uid,
                        etag=obj["etag"],
                        fingerprint=remote_fp,
                        event_date=start,
                        state="held",
                    )
                )
            continue
        actions.append(
            Action(
                "delete",
                origin,
                href,
                uid,
                etag=obj["etag"],
                fingerprint=remote_fp,
                event_date=start,
            )
        )
    for origin, row in known.items():
        if row.get("state") in _UNKNOWN_OUTCOME and origin not in mirrors and origin not in wanted:
            actions.append(
                Action(
                    "absent",
                    origin,
                    row.get("href"),
                    row.get("uid"),
                    fingerprint=row.get("fingerprint"),
                    event_date=row.get("event_date"),
                    sequence=int(row.get("sequence") or 0),
                )
            )
    return PlannedActions(actions=actions, unreadable_objects=unreadable_objects)


def _rows(engine, calendar_url: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        found = conn.execute(
            select(calendar_mirror).where(calendar_mirror.c.calendar_url == calendar_url)
        ).mappings()
        return [dict(row) for row in found]


def _save(engine, *, account_id: str, calendar_url: str, action: Action, **extra: Any) -> None:
    ledger_id, intent_id = _ids(action.origin_id, calendar_url)
    intent = CreateIntent(
        provider="caldav",
        account=account_id,
        calendar=calendar_url,
        origin_id=action.origin_id,
        ledger_id=ledger_id,
        intent_id=intent_id,
    )
    values = {
        "origin_id": action.origin_id,
        "calendar_url": calendar_url,
        "ledger_id": ledger_id,
        "intent_id": intent_id,
        "idempotency_key": create_idempotency_key(intent),
        "account_id": account_id,
        "uid": action.uid or uid_for(action.origin_id),
        "href": action.href or href_for(calendar_url, action.origin_id),
        "etag": extra.get("etag", action.etag),
        "sequence": extra.get("sequence", action.sequence),
        "fingerprint": extra.get("fingerprint", action.fingerprint),
        "pending_write": extra.get("pending_write"),
        "event_date": extra.get("event_date", action.event_date),
        "state": extra["state"],
        "revision": extra["revision"],
        "last_action": extra.get("last_action", action.op),
        "last_error": extra.get("last_error", action.error),
        "updated_at": utc_now(),
    }
    with engine.begin() as conn:
        stmt = insert(calendar_mirror).values(**values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["origin_id", "calendar_url"],
                set_={
                    key: stmt.excluded[key]
                    for key in values
                    if key not in ("origin_id", "calendar_url")
                },
            )
        )


def _revision(row: dict | None, changed: bool) -> int:
    current = int(row["revision"]) if row and row.get("revision") else 0
    return current + 1 if changed else current or 1


def _claim(account_id: str, calendar_url: str, action: Action, etag: str) -> OwnershipClaim:
    ledger_id, _intent = _ids(action.origin_id, calendar_url)
    return OwnershipClaim(
        target=ExternalObjectRef(
            provider="caldav",
            account=account_id,
            calendar=calendar_url,
            external_id=action.uid or uid_for(action.origin_id),
            etag=etag,
        ),
        origin="vibesynergy",
        origin_id=action.origin_id,
        ledger_id=ledger_id,
        verified=True,
    )


def _read_back(writer: CalendarWriter, calendar_url: str, action: Action) -> dict[str, str] | None:
    try:
        fetched = writer.get(action.href or "")
    except MirrorTimeout as exc:
        raise MirrorWriteError("readback_timeout") from exc
    except MirrorWriteError as exc:
        raise MirrorWriteError(f"readback_{exc.code}") from exc
    if fetched is None:
        return None
    try:
        parsed, key = parse_object(fetched, calendar_url)
    except ValueError as exc:
        raise MirrorWriteError("readback_malformed") from exc
    fingerprint, start, _end, _sequence = _inspect(parsed)
    if key != action.origin_id or fingerprint != action.fingerprint:
        raise MirrorWriteError("readback_mismatch")
    if start != action.event_date:
        raise MirrorWriteError("readback_mismatch")
    return fetched


def _read_back_absent(writer: CalendarWriter, action: Action) -> bool:
    try:
        fetched = writer.get(action.href or "")
    except MirrorTimeout as exc:
        raise MirrorWriteError("readback_timeout") from exc
    except MirrorWriteError as exc:
        raise MirrorWriteError(f"readback_{exc.code}") from exc
    return fetched is None


def apply_actions(
    engine,
    writer: CalendarWriter,
    actions: list[Action],
    *,
    account_id: str,
    calendar_url: str,
    ledger: list[dict[str, Any]],
) -> dict[str, int | str | None]:
    counts = {"created": 0, "updated": 0, "deleted": 0, "adopted": 0, "conflicts": 0}
    if any(item.op == "abort" for item in actions):
        return {"skipped": next(item.error for item in actions if item.op == "abort"), **counts}
    known = {row["origin_id"]: row for row in ledger}
    for action in actions:
        row = known.get(action.origin_id)
        try:
            if action.op == "remember":
                if (
                    row
                    and row.get("account_id") == account_id
                    and row.get("state") == action.state
                    and row.get("etag") == action.etag
                    and row.get("fingerprint") == action.fingerprint
                    and row.get("last_error") == action.error
                ):
                    continue
                changed = True
                _save(
                    engine,
                    account_id=account_id,
                    calendar_url=calendar_url,
                    action=action,
                    state=action.state,
                    revision=_revision(row, bool(changed and action.state == "verified")),
                    last_error=action.error,
                )
                counts["conflicts" if action.state == "conflict" else "adopted"] += 1
            elif action.op == "conflict":
                _save(
                    engine,
                    account_id=account_id,
                    calendar_url=calendar_url,
                    action=action,
                    state="conflict",
                    revision=_revision(row, False),
                    last_error=action.error,
                )
                counts["conflicts"] += 1
            elif action.op == "absent":
                _save(
                    engine,
                    account_id=account_id,
                    calendar_url=calendar_url,
                    action=action,
                    state="absent",
                    revision=_revision(row, True),
                    etag=None,
                    last_error=None,
                )
            elif action.op == "create":
                _apply_create(engine, writer, action, account_id, calendar_url, row)
                counts["created"] += 1
            elif action.op == "update":
                _apply_update(engine, writer, action, account_id, calendar_url, row)
                counts["updated"] += 1
            elif action.op == "delete":
                _apply_delete(engine, writer, action, account_id, calendar_url, row)
                counts["deleted"] += 1
        except (MirrorWriteError, ValueError) as exc:
            code = getattr(exc, "code", None) or "rejected"
            uncertain = isinstance(exc, (MirrorTimeout, PreconditionFailed)) or code.startswith(
                "readback"
            )
            _save(
                engine,
                account_id=account_id,
                calendar_url=calendar_url,
                action=action,
                state="uncertain" if uncertain else "conflict",
                pending_write=_write_intent(action) if uncertain else None,
                revision=_revision(row, False),
                last_error=code,
                etag=row.get("etag") if row else action.etag,
                fingerprint=row.get("fingerprint") if row else action.fingerprint,
            )
            counts["conflicts"] += 1
            log.info("calendar mirror %s %s %s", action.op, action.origin_id, code)
    return {"skipped": None, **counts}


def _write_intent(action: Action) -> dict:
    return {
        "op": action.op,
        "fingerprint": action.fingerprint,
        "sequence": action.sequence,
        "event_date": action.event_date.isoformat() if action.event_date else None,
    }


def _checkpoint(engine, account_id, calendar_url, action, row):
    """Commit before the network mutation, including on abrupt process interruption."""
    _save(
        engine,
        account_id=account_id,
        calendar_url=calendar_url,
        action=action,
        state="uncertain",
        pending_write=_write_intent(action),
        revision=_revision(row, False),
        fingerprint=row.get("fingerprint"),
        event_date=row.get("event_date"),
        sequence=row.get("sequence", 0),
        last_error=None,
    )


def _apply_create(engine, writer, action: Action, account_id: str, calendar_url: str, row) -> None:
    """Reserve the create key, then PUT only when a read shows the object is absent."""
    _save(
        engine,
        account_id=account_id,
        calendar_url=calendar_url,
        action=action,
        state="reserved",
        pending_write=_write_intent(action),
        revision=_revision(row, False),
        etag=None,
        last_error=None,
    )
    existing = writer.get(action.href or "")
    if existing is not None:
        parsed, key = parse_object(existing, calendar_url)
        fingerprint, _start, _end, _sequence = _inspect(parsed)
        if key != action.origin_id or fingerprint != action.fingerprint:
            raise MirrorWriteError("foreign_object")
        etag = existing["etag"]
    else:
        try:
            writer.put(action.href or "", action.body or b"", match=None)
        except PreconditionFailed:
            existing = writer.get(action.href or "")
            if existing is None:
                raise
            parsed, key = parse_object(existing, calendar_url)
            fingerprint, _start, _end, _sequence = _inspect(parsed)
            if key != action.origin_id or fingerprint != action.fingerprint:
                raise MirrorWriteError("foreign_object") from None
        fetched = _read_back(writer, calendar_url, action)
        if fetched is None:
            raise MirrorWriteError("readback_missing")
        etag = fetched["etag"]
    _save(
        engine,
        account_id=account_id,
        calendar_url=calendar_url,
        action=action,
        state="verified",
        revision=_revision(row, True),
        etag=etag,
        last_error=None,
    )


def _apply_update(engine, writer, action: Action, account_id: str, calendar_url: str, row) -> None:
    if row is None or not action.etag:
        raise MirrorWriteError("missing_baseline")
    _claim_or_refuse(account_id, calendar_url, action, action.etag, row)
    fresh = writer.get(action.href or "")
    if fresh is None or fresh["etag"] != action.etag:
        raise PreconditionFailed()
    parsed, key = parse_object(fresh, calendar_url)
    fingerprint, _start, _end, _sequence = _inspect(parsed)
    if key != action.origin_id or fingerprint != row.get("fingerprint"):
        raise MirrorWriteError("external_edit")
    _checkpoint(engine, account_id, calendar_url, action, row)
    etag = writer.put(action.href or "", action.body or b"", match=fresh["etag"])
    fetched = _read_back(writer, calendar_url, action)
    if fetched is None:
        raise MirrorWriteError("readback_missing")
    _save(
        engine,
        account_id=account_id,
        calendar_url=calendar_url,
        action=action,
        state="verified",
        revision=_revision(row, True),
        etag=fetched["etag"] or etag,
        last_error=None,
    )


def _apply_delete(engine, writer, action: Action, account_id: str, calendar_url: str, row) -> None:
    if row is None or not action.etag:
        raise MirrorWriteError("missing_baseline")
    _claim_or_refuse(account_id, calendar_url, action, action.etag, row)
    fresh = writer.get(action.href or "")
    if fresh is None:
        _save(
            engine,
            account_id=account_id,
            calendar_url=calendar_url,
            action=action,
            state="absent",
            revision=_revision(row, True),
            etag=None,
            last_error=None,
        )
        return
    if fresh["etag"] != action.etag:
        raise PreconditionFailed()
    parsed, key = parse_object(fresh, calendar_url)
    fingerprint, _start, _end, _sequence = _inspect(parsed)
    if key != action.origin_id or fingerprint != row.get("fingerprint"):
        raise MirrorWriteError("external_edit")
    _checkpoint(engine, account_id, calendar_url, action, row)
    writer.delete(action.href or "", fresh["etag"])
    if not _read_back_absent(writer, action):
        raise MirrorWriteError("readback_mismatch")
    _save(
        engine,
        account_id=account_id,
        calendar_url=calendar_url,
        action=action,
        state="absent",
        revision=_revision(row, True),
        etag=None,
        fingerprint=row.get("fingerprint"),
        last_error=None,
    )


def _claim_or_refuse(
    account_id: str, calendar_url: str, action: Action, etag: str, row: dict
) -> None:
    if row.get("account_id") != account_id:
        raise MirrorWriteError("account_mismatch")
    claim = _claim(account_id, calendar_url, action, etag)
    if claim.ledger_id != row["ledger_id"] or claim.origin_id != row["origin_id"]:
        raise MirrorWriteError("ledger_mismatch")
    if not can_write(claim):
        raise MirrorWriteError("not_owned")
    idempotency_key(claim, action.op, row["intent_id"])


def garmin_coverage(engine, today: date | None = None) -> date | None:
    """Use the latest validated sync receipt, never accumulated historical coverage."""
    today = today or utc_now().date()
    status = views.sync_status(engine, "garmin")
    row = next((d for d in status["datasets"] if d["dataset"] == "scheduled_workouts"), None)
    if not row or row.get("last_error"):
        return None
    processing = row.get("processing") or {}
    if processing.get("errors") or processing.get("pending"):
        return None
    with engine.connect() as conn:
        cursor = conn.execute(
            select(dataset_state.c.cursor).where(
                dataset_state.c.source == "garmin",
                dataset_state.c.dataset == "scheduled_workouts",
            )
        ).scalar()
        receipt = (cursor or {}).get("calendar_coverage") or {}
        try:
            start = date.fromisoformat(receipt["start"])
            end = date.fromisoformat(receipt["end"])
            completed = datetime.fromisoformat(receipt["completed_at"]).astimezone(UTC)
            ids = receipt["raw_ids"]
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not start <= today <= end
            or not timedelta(0) <= utc_now() - completed <= GARMIN_STALE_AFTER
        ):
            return None
        if not ids:
            return None
        rows = conn.execute(select(raw_item).where(raw_item.c.id.in_(ids))).mappings().all()
    if len(rows) != len(ids) or any(
        r["source"] != "garmin"
        or r["dataset"] != "scheduled_workouts"
        or not r["is_current"]
        or not r["normalised_at"]
        or r["normalise_error"]
        for r in rows
    ):
        return None
    return end


def load_desired(engine, today: date) -> tuple[list[Desired], int]:
    with engine.connect() as conn:
        rows = list(
            conn.execute(select(planned_item).where(planned_item.c.source == "garmin")).mappings()
        )
    desired: list[Desired] = []
    unkeyed = 0
    latest_create_day = today + timedelta(days=CREATE_LOOKAHEAD_DAYS)
    for row in rows:
        extra = row["extra"] or {}
        key = extra.get("native_calendar_key")
        if row["date"] >= today and not key:
            unkeyed += 1
        if not key:
            continue
        if row["date"] < today or row["date"] > latest_create_day:
            continue
        desired.append(Desired(str(key), row["date"], row["title"]))
    return desired, unkeyed


def reconcile(
    engine,
    writer: CalendarWriter,
    *,
    calendar_url: str,
    account_id: str,
    today: date,
    garmin_complete: bool,
    covered_until: date | None,
    desired: list[Desired] | None = None,
    unkeyed_future: int | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    if desired is None or unkeyed_future is None:
        loaded, counted = load_desired(engine, today)
        desired = loaded if desired is None else desired
        unkeyed_future = counted if unkeyed_future is None else unkeyed_future
    try:
        remote = writer.snapshot(calendar_url)
    except MirrorWriteError as exc:
        log.info("calendar mirror skipped %s", exc.code)
        return _skipped(exc.code)
    ledger = _rows(engine, calendar_url)
    planned = plan_actions(
        desired=desired,
        remote=remote,
        ledger=ledger,
        calendar_url=calendar_url,
        today=today,
        mutate=garmin_complete,
        deletes_allowed=garmin_complete and unkeyed_future == 0,
        covered_until=covered_until,
        account_id=account_id,
        stamp=stamp or _stamp(),
    )
    actions = planned.actions
    batch_limited = len(actions) > MAX_ACTIONS
    if batch_limited:
        actions = actions[:MAX_ACTIONS]
    result = apply_actions(
        engine,
        writer,
        actions,
        account_id=account_id,
        calendar_url=calendar_url,
        ledger=ledger,
    )
    if result["skipped"] is None and batch_limited:
        result["skipped"] = "action_batch_limited"
    result["ignored_unreadable"] = planned.unreadable_objects
    result["batch_limited"] = batch_limited
    log.info(
        "calendar mirror created=%s updated=%s deleted=%s adopted=%s conflicts=%s "
        "skipped=%s unreadable=%s batch_limited=%s",
        result["created"],
        result["updated"],
        result["deleted"],
        result["adopted"],
        result["conflicts"],
        result["skipped"],
        result["ignored_unreadable"],
        result["batch_limited"],
    )
    return result


def preview(
    engine,
    writer: CalendarWriter,
    *,
    calendar_url: str,
    account_id: str,
    today: date,
    limit: int = PREVIEW_ACTIONS,
) -> dict[str, Any]:
    desired, unkeyed_future = load_desired(engine, today)
    covered_until = garmin_coverage(engine, today)
    garmin_complete = covered_until is not None
    deletes_allowed = garmin_complete and unkeyed_future == 0
    try:
        remote = writer.snapshot(calendar_url)
    except MirrorWriteError as exc:
        return {
            "ready_for_writes": False,
            "garmin_complete": garmin_complete,
            "deletes_allowed": deletes_allowed,
            "covered_until": covered_until.isoformat() if covered_until else None,
            "unkeyed_future": unkeyed_future,
            "counts": {"create": 0, "update": 0, "delete": 0, "conflict": 0},
            "total_actions": 0,
            "action_limit": MAX_ACTIONS,
            "batch_limited": False,
            "ignored_unreadable": 0,
            "error": exc.code,
            "actions": [],
            "truncated": False,
        }
    planned = plan_actions(
        desired=desired,
        remote=remote,
        ledger=_rows(engine, calendar_url),
        calendar_url=calendar_url,
        today=today,
        mutate=garmin_complete,
        deletes_allowed=deletes_allowed,
        covered_until=covered_until,
        account_id=account_id,
        stamp=_stamp(),
    )
    if any(action.op == "abort" for action in planned.actions):
        return {
            "ready_for_writes": False,
            "garmin_complete": garmin_complete,
            "deletes_allowed": deletes_allowed,
            "covered_until": covered_until.isoformat() if covered_until else None,
            "unkeyed_future": unkeyed_future,
            "counts": {"create": 0, "update": 0, "delete": 0, "conflict": 0},
            "total_actions": 0,
            "action_limit": MAX_ACTIONS,
            "batch_limited": False,
            "ignored_unreadable": planned.unreadable_objects,
            "error": next(action.error for action in planned.actions if action.op == "abort"),
            "actions": [],
            "truncated": False,
        }
    # Show the writes that adoption/recovery will authorize on the following pass.
    # No provider or ledger write occurs while projecting these checkpoints.
    projected = {row["origin_id"]: row for row in _rows(engine, calendar_url)}
    for action in planned.actions:
        if action.op == "remember":
            projected[action.origin_id] = {
                "origin_id": action.origin_id,
                "account_id": account_id,
                "state": action.state,
                "etag": action.etag,
                "fingerprint": action.fingerprint,
                "event_date": action.event_date,
                "last_error": action.error,
            }
    projected_plan = plan_actions(
        desired=desired,
        remote=remote,
        ledger=list(projected.values()),
        calendar_url=calendar_url,
        today=today,
        mutate=garmin_complete,
        deletes_allowed=deletes_allowed,
        covered_until=covered_until,
        account_id=account_id,
        stamp=_stamp(),
    )
    projected_actions = {a.origin_id: a for a in projected_plan.actions}
    # Conflicts saved with remember are still visible, including unchanged conflicts.
    for origin, row in projected.items():
        if row.get("account_id") == account_id and row.get("state") == "conflict":
            projected_actions[origin] = Action(
                "conflict",
                origin,
                event_date=row.get("event_date"),
                error=row.get("last_error"),
            )
    write_actions = [
        action
        for action in projected_actions.values()
        if action.op in {"create", "update", "delete", "conflict"}
    ]
    counts = Counter(action.op for action in write_actions)
    write_actions.sort(
        key=lambda action: (
            action.event_date or date.max,
            action.origin_id,
            action.op,
        )
    )
    titles = {item.origin_id: item.title for item in desired}
    for obj in remote:
        try:
            parsed, key = parse_object(obj, calendar_url)
        except ValueError:
            continue
        if key and key not in titles:
            titles[key] = str(parsed.walk("VEVENT")[0].get("SUMMARY") or "")
    limited = write_actions[: max(0, limit)]
    return {
        "ready_for_writes": garmin_complete,
        "garmin_complete": garmin_complete,
        "deletes_allowed": deletes_allowed,
        "covered_until": covered_until.isoformat() if covered_until else None,
        "unkeyed_future": unkeyed_future,
        "counts": {
            "create": counts.get("create", 0),
            "update": counts.get("update", 0),
            "delete": counts.get("delete", 0),
            "conflict": counts.get("conflict", 0),
        },
        "total_actions": len(write_actions),
        "action_limit": MAX_ACTIONS,
        "batch_limited": len(write_actions) > MAX_ACTIONS,
        "ignored_unreadable": planned.unreadable_objects,
        "error": None,
        "actions": [
            {
                "op": action.op,
                "title": titles.get(action.origin_id),
                "origin_id": action.origin_id,
                "event_date": action.event_date.isoformat() if action.event_date else None,
                "error": action.error,
            }
            for action in limited
        ],
        "truncated": len(write_actions) > len(limited),
    }


def run(rt) -> dict[str, Any]:
    """One reconcile. Off unless Settings → Sync between sources has copying turned on."""
    if not account.mirror_enabled(rt.settings):
        return _skipped("disabled")
    if not account.writes_allowed(rt.settings):
        return _skipped("read_only_account")
    target = account.garmin_target(rt.settings)
    if not target:
        return _skipped("no_target")
    stored = account.store_for(rt.settings).get(account.ACCOUNT) or {}
    from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair

    request_timeout = requests_timeout_pair(http_timeout_seconds(rt.settings, "caldav"))
    writer = CalendarWriter(
        account.client_for(account.store_for(rt.settings), stored, request_timeout=request_timeout)
    )
    with (
        SecretStore(rt.settings.secrets_dir, "garmin").lock("calendar-read-write"),
        SecretStore(rt.settings.secrets_dir, "caldav").lock("mirror"),
    ):
        covered_until = garmin_coverage(rt.engine, today_in(rt.settings.timezone))
        return reconcile(
            rt.engine,
            writer,
            calendar_url=target,
            account_id=account_id(stored),
            today=today_in(rt.settings.timezone),
            garmin_complete=covered_until is not None,
            covered_until=covered_until,
        )


def summary(engine) -> dict[str, Any]:
    """Ownership and revisions for query/API/MCP. No titles, bodies, hrefs or ETags."""
    bound = getattr(engine, "conn", None)
    cache = bound.info.setdefault("vibesynergy_read_cache", {}) if bound is not None else None
    if cache is not None and "calendar_mirror_summary" in cache:
        return cache["calendar_mirror_summary"]
    built = _summary(engine)
    if cache is not None:
        cache["calendar_mirror_summary"] = built
    return built


def _summary(engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(select(calendar_mirror)).mappings()]
    counts: dict[str, int] = {}
    revisions = []
    for row in sorted(rows, key=lambda item: (item["origin_id"], item["calendar_url"])):
        counts[row["state"]] = counts.get(row["state"], 0) + 1
        if row["state"] == "absent":
            continue
        revisions.append(
            {
                "origin_id": row["origin_id"],
                "state": row["state"],
                "revision": row["revision"],
                "event_date": row["event_date"].isoformat() if row["event_date"] else None,
                "last_action": row["last_action"],
                "last_error": row["last_error"],
            }
        )
    return {"counts": counts, "revisions": revisions[:200]}


def ledger_index(engine) -> dict[str, dict[str, Any]]:
    """One visible row per Garmin key, preferring a verified mirror."""
    best: dict[str, dict[str, Any]] = {}
    for row in summary(engine)["revisions"]:
        current = best.get(row["origin_id"])
        if current is None or (row["state"] == "verified" and current["state"] != "verified"):
            best[row["origin_id"]] = row
    return best
