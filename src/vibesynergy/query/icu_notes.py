"""Copy the day's diary notes to intervals.icu, only where they stay private (opt-in).

intervals.icu has no private note on an activity: the description and the activity chat
follow the activity's visibility. Wellness ``comments`` (one text per day) can be private:
the athlete setting "Private wellness data" (``private_wellness_keys`` in the API) hides the
listed wellness fields from everyone except the athlete and their coaches. So:

- Off by default; the portal preference ``intervals_icu.private_notes`` switches it on.
- Before every write the athlete settings are read again. Unless ``comments`` is among the
  private wellness fields, nothing is sent.
- Only notes the athlete wrote (not the copies imported from Garmin and Strava), one line each.
- A comment on intervals.icu is only replaced when it is empty or still exactly what VibeSynergy
  sent last time (read from the audit log). Anything else is kept.

Garmin has no private note field either (the activity description follows the activity's
privacy), so notes stay in VibeSynergy there.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, desc, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import audit_log, note

log = logging.getLogger(__name__)

PREF = "intervals_icu.private_notes"
ACTION = "icu.wellness_comments"
FIELD = "comments"
# Notes that are copies of what Garmin or Strava already hold, not the athlete's own words.
IMPORTED = frozenset({"garmin", "strava"})
RECENT_DAYS = 7


class NotPrivate(Exception):
    """intervals.icu would show the comment to followers: nothing was sent."""


def enabled(data_dir: Path) -> bool:
    from vibesynergy.portal import prefs

    return prefs.load(data_dir).get(PREF) is True


def _configured() -> bool:
    from vibesynergy.sources.intervals_icu.client import account_label

    return account_label() is not None


def comments_private(athlete: dict[str, Any]) -> bool:
    keys = athlete.get("private_wellness_keys") or []
    return isinstance(keys, list) and FIELD in keys


def day_text(engine: Engine, day: date) -> str:
    """The athlete's own notes that start on ``day``, one per line, tags in brackets."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(note.c.text, note.c.tags, note.c.source)
            .where(note.c.start_date == day)
            .order_by(note.c.created_at)
        ).all()
    lines = []
    for text, tags, source in rows:
        if source in IMPORTED:
            continue
        tags = [str(tag).replace("_", " ") for tag in tags or []]
        lines.append(f"{text} [{', '.join(tags)}]" if tags else text)
    return "\n".join(lines)


def last_sent(engine: Engine, day: date) -> str | None:
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_log.c.detail)
            .where(audit_log.c.action == ACTION, audit_log.c.target == day.isoformat())
            .order_by(desc(audit_log.c.id))
            .limit(1)
        ).all()
    if not rows:
        return None
    after = (rows[0].detail or {}).get("after") or {}
    value = after.get(FIELD)
    return value if isinstance(value, str) else None


def push_day(engine: Engine, day: date, client) -> str:
    """Write one day's notes to wellness comments. Returns sent, same, kept or not_private."""
    from vibesynergy.mcp.identity import current_author

    if not comments_private(client.get_athlete()):
        raise NotPrivate
    wanted = day_text(engine, day)
    previous = last_sent(engine, day)
    current = client.get_wellness(day).get(FIELD) or ""
    if current == wanted:
        return "same"
    if current and current != previous:
        return "kept"  # written on intervals.icu (or elsewhere): never replaced
    if not wanted and previous is None:
        return "same"
    body = {FIELD: wanted}
    client.put_wellness(day, body)
    with engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="vibesynergy",
                action=ACTION,
                detail={"date": day.isoformat(), "before": {FIELD: current}, "after": body},
                author=current_author(),
                scope="diary.write",
                target=day.isoformat(),
                outcome="ok",
            )
        )
    return "sent"


def schedule(engine: Engine, data_dir: Path, days: set[date]) -> None:
    """After a note changed: copy those days in the background when switched on."""
    if not days or not enabled(data_dir) or not _configured():
        return
    threading.Thread(
        target=lambda: _push_quietly(engine, sorted(days)), name="icu-notes", daemon=True
    ).start()


def _push_quietly(engine: Engine, days: list[date], client=None) -> int:
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    sent = 0
    try:
        client = client or IntervalsIcuClient.from_env()
        for day in days:
            if push_day(engine, day, client) == "sent":
                sent += 1
    except NotPrivate:
        log.info("notes not copied: comments are not private wellness data on intervals.icu")
    except Exception as exc:
        log.warning("notes not copied to intervals.icu: %s", type(exc).__name__)
    return sent


def retry_recent(engine: Engine, data_dir: Path, today: date, client=None) -> int:
    """After an intervals.icu sync: copy recent days whose notes differ from what was sent."""
    if not enabled(data_dir) or (client is None and not _configured()):
        return 0
    days = [today - timedelta(days=n) for n in range(RECENT_DAYS)]
    changed = [day for day in days if day_text(engine, day) != (last_sent(engine, day) or "")]
    if not changed:
        return 0
    return _push_quietly(engine, changed, client)


def status(client) -> dict[str, Any]:
    """Whether intervals.icu keeps wellness comments private, for the settings page."""
    return {"private": comments_private(client.get_athlete())}
