"""Copy a saved morning check-in to intervals.icu wellness.

intervals.icu stores these six subjective scores on a 1-4 scale. For every one of
them, 1 is good and 4 is bad: mood runs Great to Grumpy, motivation Extreme to Low,
sleep quality the same way, and soreness, fatigue and stress Low to Extreme.

VibeSynergy's check-in is 1-5. Soreness and stress already rise toward worse, so they
only compress (1, 2, 3, 4, 5) to (1, 2, 3, 3, 4). Mood, motivation and sleep quality
rise toward better, so those are reversed (5, 4, 3, 2, 1) to (1, 2, 2, 3, 4).

VibeSynergy does not collect fatigue, so that field is omitted. A fatigue score passed
to the mapper follows soreness and stress. A field that was empty is cleared with
-1, which is what intervals.icu uses, not null.

The PUT body contains only these fields. Garmin values such as hrv, restingHR,
sleepSecs and sleepScore are never sent, so a partial update does not clear them.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.config import Settings, load_settings
from vibesynergy.core.timeutil import utc_now
from vibesynergy.core.user_datetime import format_user_date
from vibesynergy.mcp.identity import current_author
from vibesynergy.query.icu_plan import IcuWriteError, _audit, _feed
from vibesynergy.sources.intervals_icu.client import (
    API_KEY_ENV,
    IntervalsIcuClient,
)
from vibesynergy.store.schema import checkin, icu_checkin_push

log = logging.getLogger(__name__)

# 1-5 where a higher VibeSynergy score is already worse (1 good, 4 bad on intervals.icu).
TO_ICU = {1: 1, 2: 2, 3: 3, 4: 3, 5: 4}
# Mood, motivation and sleep quality: VibeSynergy 5 is best, intervals.icu 1 is best.
TO_ICU_REVERSED = {1: 4, 2: 3, 3: 2, 4: 2, 5: 1}
REVERSED_FIELDS = frozenset({"mood", "motivation", "sleepQuality"})
# intervals.icu clears a wellness field with -1, not null.
CLEAR_FIELD = -1

CHECKIN_TO_WELLNESS = {
    "mood": "mood",
    "soreness": "soreness",
    "fatigue": "fatigue",
    "stress": "stress",
    "motivation": "motivation",
    "sleep_quality": "sleepQuality",
}
WELLNESS_FIELDS = frozenset(CHECKIN_TO_WELLNESS.values())
# Never written. A partial PUT leaves whatever intervals.icu already stored.
FORBIDDEN_FIELDS = frozenset(
    {
        "hrv",
        "hrvSDNN",
        "restingHR",
        "sleepSecs",
        "sleepScore",
        "avgSleepingHR",
        "weight",
        "bodyFat",
        "kcalConsumed",
        "atl",
        "ctl",
    }
)

CONFIG_KEY = "checkin_wellness"


def to_icu(field: str, score: int) -> int:
    """Map one 1-5 check-in score onto intervals.icu's 1-4 scale for ``field``."""
    table = TO_ICU_REVERSED if field in REVERSED_FIELDS else TO_ICU
    if score not in table:
        raise IcuWriteError("check-in scores are 1 to 5")
    return table[score]


def wellness_fields(scores: dict[str, Any]) -> dict[str, int]:
    """The fields to send. Unset scores are left out so the PUT does not clear them."""
    body: dict[str, int] = {}
    for source, target in CHECKIN_TO_WELLNESS.items():
        value = scores.get(source)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        body[target] = to_icu(target, value)
    extra = set(body) - WELLNESS_FIELDS
    if extra or set(body) & FORBIDDEN_FIELDS:
        raise IcuWriteError("refusing to write fields that are not the check-in")
    return body


def enabled(settings: Settings | None = None) -> bool:
    """On unless ``[sources.intervals_icu] checkin_wellness`` is false."""
    settings = settings or load_settings()
    return bool(settings.source_config("intervals_icu").get(CONFIG_KEY, True))


def schedule_checkin_wellness(engine: Engine, day: date) -> None:
    """Remember this date and try the write without delaying the check-in save."""
    if not enabled():
        return
    if not _configured():
        log.info("check-in was saved; intervals.icu wellness was not sent (not set up)")
        return
    try:
        _mark_pending(engine, day)
    except Exception as exc:
        _log_failure(exc)
        return
    _start(lambda: _push_quietly(engine, day))


def retry_pending(
    engine: Engine, settings: Settings, client: IntervalsIcuClient | None = None
) -> int:
    """Push check-ins that failed or were saved again. Returns how many were sent."""
    if not enabled(settings):
        return 0
    with engine.connect() as conn:
        dates = [
            row.date
            for row in conn.execute(
                select(icu_checkin_push.c.date, icu_checkin_push.c.checkin_updated_at)
                .join(checkin, checkin.c.date == icu_checkin_push.c.date)
                .where(
                    (icu_checkin_push.c.last_error.is_not(None))
                    | (icu_checkin_push.c.pushed_at.is_(None))
                    | (checkin.c.updated_at > icu_checkin_push.c.checkin_updated_at)
                )
            )
        ]
    if not dates:
        return 0
    if not _configured() and client is None:
        return 0
    own = client or IntervalsIcuClient.from_env()
    sent = 0
    for day in dates:
        if _push_quietly(engine, day, own):
            sent += 1
    return sent


def push_checkin_wellness(
    engine: Engine,
    day: date,
    client: IntervalsIcuClient | None = None,
) -> dict[str, Any] | None:
    """PUT one day's check-in. Raises on failure; callers that must not block catch it."""
    own = client or IntervalsIcuClient.from_env()
    with engine.connect() as conn:
        row = (
            conn.execute(select(checkin).where(checkin.c.subject_id == 1, checkin.c.date == day))
            .mappings()
            .first()
        )
    if row is None:
        return None
    saved = dict(row)
    stamp = saved["updated_at"]
    body = wellness_fields(saved)
    if not body:
        _mark_done(engine, day, stamp, None)
        return None
    current = own.get_wellness(day)
    before = {key: _previous(current, key) for key in body}
    own.put_wellness(day, body)
    _guard(body)
    _guard(before)
    who = current_author()
    reason = f"Check-in for {format_user_date(day)} is saved."
    with engine.begin() as conn:
        audit_id = _audit(
            conn,
            action="icu.wellness",
            reason=reason,
            before=before,
            after=body,
            event_id=day.isoformat(),
            author=who,
            extra={"date": day.isoformat()},
        )
        _feed(
            conn,
            reason=reason,
            day=day,
            author=who,
            meta={"audit_id": audit_id, "action": "icu.wellness", "event_id": day.isoformat()},
        )
        updated = conn.execute(
            icu_checkin_push.update()
            .where(
                icu_checkin_push.c.date == day,
                icu_checkin_push.c.checkin_updated_at == stamp,
            )
            .values(pushed_at=utc_now(), last_error=None, sent=body)
        ).rowcount
    if not updated:
        _mark_done_if_current(engine, day, stamp, body)
    return {"date": day.isoformat(), "sent": body, "before": before, "audit_id": audit_id}


def restore_wellness(
    engine: Engine,
    client: IntervalsIcuClient,
    detail: dict,
    *,
    reason: str,
    author: str,
    audit_id: int,
) -> dict[str, Any]:
    """PUT the previous check-in fields back. Other wellness fields stay untouched."""
    text = detail.get("date") or detail.get("event_id")
    day = date.fromisoformat(str(text))
    raw = detail.get("before") or {}
    if not isinstance(raw, dict):
        raise IcuWriteError("That check-in change has no previous values.")
    before = {key: CLEAR_FIELD if value is None else value for key, value in raw.items()}
    _guard(before)
    client.put_wellness(day, before)
    with engine.begin() as conn:
        new_id = _audit(
            conn,
            action="icu.revert",
            reason=reason,
            before=detail.get("after"),
            after=before,
            event_id=day.isoformat(),
            author=author,
            extra={"reverts": audit_id, "date": day.isoformat()},
        )
        _feed(
            conn,
            reason=reason,
            day=day,
            author=author,
            meta={"audit_id": new_id, "action": "icu.revert", "event_id": day.isoformat()},
        )
        row = conn.execute(
            select(checkin.c.updated_at).where(checkin.c.subject_id == 1, checkin.c.date == day)
        ).scalar_one_or_none()
        if row is not None:
            conn.execute(
                icu_checkin_push.update()
                .where(icu_checkin_push.c.date == day)
                .values(
                    checkin_updated_at=row,
                    pushed_at=utc_now(),
                    last_error=None,
                    sent=before,
                )
            )
    return {"date": day.isoformat(), "sent": before, "audit_id": new_id, "action": "icu.revert"}


def _start(fn) -> None:
    threading.Thread(target=fn, name="icu-checkin-wellness", daemon=True).start()


def _push_quietly(engine: Engine, day: date, client: IntervalsIcuClient | None = None) -> bool:
    try:
        return push_checkin_wellness(engine, day, client) is not None
    except Exception as exc:
        _log_failure(exc)
        try:
            _mark_error(engine, day, exc)
        except Exception:
            _log_failure(exc)
        return False


def _configured() -> bool:
    from vibesynergy.sources.intervals_icu.client import account_label

    return account_label() is not None


def _mark_pending(engine: Engine, day: date) -> None:
    with engine.connect() as conn:
        stamp = conn.execute(
            select(checkin.c.updated_at).where(checkin.c.subject_id == 1, checkin.c.date == day)
        ).scalar_one_or_none()
    if stamp is None:
        return
    with engine.begin() as conn:
        conn.execute(
            insert(icu_checkin_push)
            .values(
                date=day,
                checkin_updated_at=stamp,
                pushed_at=None,
                last_error="pending",
                sent=None,
            )
            .on_conflict_do_update(
                index_elements=["date"],
                set_={"checkin_updated_at": stamp, "last_error": "pending"},
            )
        )


def _mark_done_if_current(engine: Engine, day: date, stamp: datetime, sent: dict) -> None:
    with engine.connect() as conn:
        current = conn.execute(
            select(checkin.c.updated_at).where(checkin.c.subject_id == 1, checkin.c.date == day)
        ).scalar_one_or_none()
    if current != stamp:
        _mark_pending(engine, day)
        return
    _mark_done(engine, day, stamp, sent)


def _mark_done(engine: Engine, day: date, stamp: datetime, sent: dict | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(icu_checkin_push)
            .values(
                date=day,
                checkin_updated_at=stamp,
                pushed_at=utc_now(),
                last_error=None,
                sent=sent,
            )
            .on_conflict_do_update(
                index_elements=["date"],
                set_={
                    "checkin_updated_at": stamp,
                    "pushed_at": utc_now(),
                    "last_error": None,
                    "sent": sent,
                },
            )
        )


def _mark_error(engine: Engine, day: date, exc: Exception) -> None:
    message = _redact(f"{type(exc).__name__}: {exc}")[:500]
    with engine.begin() as conn:
        conn.execute(
            icu_checkin_push.update()
            .where(icu_checkin_push.c.date == day)
            .values(last_error=message)
        )


def _previous(current: dict, key: str) -> int:
    """The value to put back. An empty field is cleared with -1."""
    value = current.get(key)
    if value is None:
        return CLEAR_FIELD
    return value


def _guard(body: dict) -> None:
    extra = set(body) - WELLNESS_FIELDS
    if extra or set(body) & FORBIDDEN_FIELDS:
        raise IcuWriteError("refusing to write fields that are not the check-in")


def _log_failure(exc: Exception) -> None:
    log.warning("check-in was saved; intervals.icu wellness was not: %s", _redact(str(exc)))


def _redact(text: str) -> str:
    secrets = []
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        secrets.append(key)
        secrets.append(base64.b64encode(f"API_KEY:{key}".encode()).decode())
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text
