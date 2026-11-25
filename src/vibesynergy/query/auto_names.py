"""The AI coach names new workouts as part of its insight (#114 phase 7a, #188).

Settings → Publishing and sync, card "Publish workouts": "Give workouts a name automatically"
(``[publication] auto_name_enabled``). **On by default**; a saved false switches it off, and
then VibeSynergy leaves names alone.

**The name.** When the AI coach writes the public insight of a workout
(``vibesynergy_coach_write`` with ``activity_id`` and ``workout_name``), it writes a short
name too, from everything it knows: what was actually done (sport, indoor or outdoor,
structure, intensity, distance or duration), the plan only as what the session was meant to
be, and the place of an outdoor workout. The instruction is part of the insight's harness
rules (``coach_runner.prompts.INSIGHT_RULES``). There is no rule-made name any more (#188:
the plan title of a planned MTB ride on a Zwift ride): without a name from the AI coach the
workout keeps the name it came in with from Garmin or Strava.

**Stored like any name.** The name is a version in the workout's text history
(``workout_texts``, source ``ai``, kind ``auto``, shown as "AI coach · Named
automatically"), so the two-way sync passes it on to Strava and Garmin for a published
workout, exactly like a name typed in VibeSynergy. Only names go out this way; the Garmin
calendar is never touched. A rewritten insight (a redo, a changed text, a correction) may
name the workout again.

**Locked once you change it.** A name you changed yourself (in VibeSynergy, or on Strava or
Garmin and pulled in by the sync) is locked: ``workout_texts.name_lock`` derives it from the
history, and the AI coach never writes over it again. The name the workout came in with from
Garmin or Strava (the device default) is no change of yours, so it may be replaced.

**Only new workouts.** Workouts that started from the moment the setting first took effect
(``[publication] auto_name_since``, written once by the first derive with the setting on, and
again when you switch it back on in Settings), so the history is never renamed. There is no
further window: an insight may come days later (you publish the workout later, or ask for a
new insight), and its name still counts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from vibesynergy.store.schema import activity_unified

log = logging.getLogger(__name__)

AUTHOR = "ai-coach"


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


def from_insight(rt, activity_id: str, name: str | None, *, now: datetime | None = None) -> str:
    """Store the name the AI coach wrote with its insight, when it may.

    Returns what happened: ``named``, ``kept`` (the same name, or none given), ``off`` (the
    setting is off), ``old`` (the workout started before the cutoff, or has no start) or
    ``locked`` (you changed the name yourself)."""
    from vibesynergy.query import workout_texts

    clean = " ".join(str(name or "").split())[: workout_texts.MAX_NAME]
    if not clean:
        return "kept"
    if not enabled(rt):
        return "off"
    cutoff = _start_since(rt, now or datetime.now(UTC))
    group = workout_texts.texts(rt.engine, activity_id)["activity_id"]
    with rt.engine.connect() as conn:
        start = conn.scalar(
            select(activity_unified.c.start_utc).where(activity_unified.c.group_id == group)
        )
    if start is None:
        return "old"
    if (start if start.tzinfo else start.replace(tzinfo=UTC)) < cutoff:
        return "old"
    current = workout_texts.texts(rt.engine, group)["name"]
    if current.get("lock"):
        return "locked"
    if clean == current.get("text"):
        return "kept"
    workout_texts.set_auto_name(rt.engine, group, clean, source="ai", author=AUTHOR)
    return "named"


def after_derive(rt) -> None:
    """Called after every derive: sets the cutoff the first time the setting is in effect,
    so the workouts that come in from then on are the new ones. Never fails the sync."""
    try:
        if enabled(rt):
            _start_since(rt, datetime.now(UTC))
    except Exception:
        log.exception("setting the cutoff for automatic names failed")
