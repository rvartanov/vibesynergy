"""Give new workouts a name automatically (#114, phase 7a).

Settings → Publishing and sync, card "Publish workouts": "Give workouts a name
automatically" (``[publication] auto_name_enabled``). **On by default**; a saved false
switches it off, and then VibeSynergy leaves names alone.

**The name.** The title of the VibeSynergy plan session the workout matched (``plan.plan``:
one session of that day and sport, done or done differently); else sport and part of day,
then the place Garmin gives the workout (``activity_weather.place``), such as "Ochtendloop
Leeuwarden" or "Morning run Leeuwarden", in the language of the published text
(``publication_auto.language``). Without a known place the name is only "Ochtendloop".
Part of day by the local start time: night before 6, morning before 12, afternoon before 18,
evening after.

**Stored like any name.** The name is a version in the workout's text history
(``workout_texts``, source ``vs``, kind ``auto``), so the two-way sync passes it on to
Strava and Garmin for a published workout, exactly like a name typed in VibeSynergy.
Only names go out this way; the Garmin calendar is never touched.

**Locked once you change it.** A name you changed yourself (in VibeSynergy, or on Strava or
Garmin and pulled in by the sync) is locked: ``workout_texts.name_lock`` derives it from the
history, and this module never writes over it again, also not when the workout is analysed
again. The name the workout came in with from Garmin or Strava (the device default) is no
change of yours, so it may be replaced once.

**Only new workouts.** Workouts that started from the moment the setting first took effect
(``[publication] auto_name_since``, written once the first time this runs with the setting
on, and again when you switch it back on in Settings), and within ``LOOKBACK``, so the
history is never renamed and a workout is named again only while it is new (a plan match
that arrives later still changes an unlocked automatic name).

It runs after every derive (``Runtime.derive``), before the publication run picks the workout
up; a problem here never fails the sync.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from vibesynergy.store.schema import activity_unified

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=2)
# (last hour, part of day) by the local start hour.
_PARTS: tuple[tuple[int, str], ...] = ((6, "night"), (12, "morning"), (18, "afternoon"))
# The name per part of day and sport family. Full phrases, because Dutch writes them as one
# word ("Ochtendloop"); the Dutch is in the catalogue (static/i18n/nl.json).
PHRASES: dict[tuple[str, str], str] = {
    ("night", "run"): "Night run",
    ("morning", "run"): "Morning run",
    ("afternoon", "run"): "Afternoon run",
    ("evening", "run"): "Evening run",
    ("night", "ride"): "Night ride",
    ("morning", "ride"): "Morning ride",
    ("afternoon", "ride"): "Afternoon ride",
    ("evening", "ride"): "Evening ride",
    ("night", "swim"): "Night swim",
    ("morning", "swim"): "Morning swim",
    ("afternoon", "swim"): "Afternoon swim",
    ("evening", "swim"): "Evening swim",
    ("night", "walk"): "Night walk",
    ("morning", "walk"): "Morning walk",
    ("afternoon", "walk"): "Afternoon walk",
    ("evening", "walk"): "Evening walk",
    ("night", "strength"): "Night strength training",
    ("morning", "strength"): "Morning strength training",
    ("afternoon", "strength"): "Afternoon strength training",
    ("evening", "strength"): "Evening strength training",
    ("night", "workout"): "Night workout",
    ("morning", "workout"): "Morning workout",
    ("afternoon", "workout"): "Afternoon workout",
    ("evening", "workout"): "Evening workout",
}


def _settings(rt) -> dict[str, Any]:
    from vibesynergy.core.config import load_settings

    return load_settings(rt.settings.config_path).publication


def enabled(rt) -> bool:
    """On unless Settings saved it off."""
    return _settings(rt).get("auto_name_enabled", True) is not False


def since(rt) -> datetime | None:
    raw = _settings(rt).get("auto_name_since")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _start_since(rt, now: datetime) -> datetime:
    """The cutoff; written to the config the first time the setting is in effect."""
    found = since(rt)
    if found is not None:
        return found
    from vibesynergy.core import config_edit

    config_edit.apply_changes(
        rt.settings.config_path, {"publication": {"auto_name_since": now.isoformat()}}
    )
    return now


def part_of_day(hour: int) -> str:
    return next((part for last, part in _PARTS if hour < last), "evening")


def _family(sport: str | None) -> str:
    from vibesynergy.query import plan

    found = plan.family(sport)
    if found == "strength":
        # The plan's strength family also holds yoga and pilates: only strength is named so.
        return "strength" if "strength" in (sport or "").lower() else "workout"
    return found or "workout"


def _local_start(rt, start: datetime, offset_s: int | None) -> datetime:
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    if offset_s is not None:
        return start.astimezone(timezone(timedelta(seconds=int(offset_s))))
    try:
        return start.astimezone(ZoneInfo(rt.settings.timezone))
    except Exception:  # an unknown zone name: UTC
        return start


def plan_title(rt, group: str, day: date) -> str | None:
    """The title of the VibeSynergy plan session this workout was matched to, if any."""
    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import plan

    found = plan.plan(
        rt.engine, Window(day, day), day, include_calendar=False, include_sync_status=False
    )
    for item in found.get("items") or []:
        if not item.get("session_id"):
            continue  # only sessions of VibeSynergy's own plan
        if any(d.get("id") == group for d in item.get("done_by") or []):
            title = " ".join(str(item.get("title") or "").split())
            return title or None
    return None


def name_for(rt, group: str, language: str) -> str | None:
    """The automatic name of a workout (see the module text). None when it has no start."""
    from vibesynergy.i18n import translator
    from vibesynergy.query import activity_weather

    with rt.engine.connect() as conn:
        row = conn.execute(
            select(
                activity_unified.c.start_utc,
                activity_unified.c.tz_offset_s,
                activity_unified.c.local_date,
                activity_unified.c.sport,
            ).where(activity_unified.c.group_id == group)
        ).first()
    if row is None or row.start_utc is None:
        return None
    titled = plan_title(rt, group, row.local_date)
    if titled:
        return titled
    local = _local_start(rt, row.start_utc, row.tz_offset_s)
    base = translator(language)(PHRASES[(part_of_day(local.hour), _family(row.sport))])
    place = activity_weather.place(rt, group)
    return f"{base} {place}" if place else base


def run(rt, *, now: datetime | None = None) -> dict[str, Any]:
    """Name each new, unlocked workout. Returns ``{named, locked, kept}``."""
    from vibesynergy.query import publication_auto, workout_texts

    summary = {"named": 0, "locked": 0, "kept": 0}
    if not enabled(rt):
        return summary
    now = now or datetime.now(UTC)
    start = max(_start_since(rt, now), now - LOOKBACK)
    with rt.engine.connect() as conn:
        groups = list(
            conn.execute(
                select(activity_unified.c.group_id)
                .where(activity_unified.c.start_utc >= start)
                .order_by(activity_unified.c.start_utc)
            ).scalars()
        )
    language = publication_auto.language(rt) if groups else "nl"
    for group in groups:
        current = workout_texts.texts(rt.engine, group)["name"]
        if current.get("lock"):
            summary["locked"] += 1
            continue
        name = name_for(rt, group, language)
        if not name or " ".join(name.split()) == current.get("text"):
            summary["kept"] += 1
            continue
        workout_texts.set_auto_name(rt.engine, group, name[: workout_texts.MAX_NAME])
        summary["named"] += 1
    return summary


def after_derive(rt) -> None:
    """Called after every derive; never lets a problem here fail the sync."""
    try:
        run(rt)
    except Exception:
        log.exception("giving new workouts a name failed")
