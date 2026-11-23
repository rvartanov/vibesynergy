"""The AI-coach insight follows the workout's texts in VibeSynergy (#114, phase 4).

The insight run reads VibeSynergy's own texts of the workout (``context.activity_view``:
the name, the public text, and the private text as context only). This module decides
*when* it runs beyond the ``activity.published`` event and the evening routine, which stay
as they were:

- **Publish tick.** Publishing a workout from VibeSynergy (the tick on the workout page, or
  the automatic run for "new workouts: public") queues the insight when the workout has none
  yet, so it is written with the athlete's text already in VibeSynergy (``after_publish``,
  run key ``publish-<group>``: once per workout). The automatic block update
  (``publication_auto``) then puts the line into the published section.
- **Text changed after the insight.** When the name, the public text or the private text
  gets a new version after the current insight was written (edited or restored in
  VibeSynergy, or taken over from Strava or Garmin by the two-way sync), the insight is
  written again for that one workout (the #158 redo). It waits until the texts have been
  quiet for ``DEBOUNCE``, so a run of edits gives one rewrite. Seeds and versions that lost
  in the sync do not count.
- **Correction.** The athlete's comment on the insight (``workout_texts.set_correction``)
  starts a rewrite at once; the run gets the correction in its context
  (``context._insight_rewrite``), private.

Each state of the texts gets one run: the run key is ``text-<group>-<newest version id>``,
so the same change is never rewritten twice, and a run that wrote nothing is not retried.
At most ``per_day`` rewrites per workout per local day (``[coach_runner]
insight_rewrites_per_day``, default ``DEFAULT_PER_DAY``; 0 switches them off). A change that
waits for the limit is picked up the next day, within ``LOOKBACK``. Nothing runs while the
runner is off, over its monthly cap, or the insight task is switched off.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from vibesynergy.core.timeutil import utc_now

log = logging.getLogger(__name__)

DEBOUNCE = timedelta(minutes=5)  # as the text sync: a few quiet minutes after the last edit
LOOKBACK = timedelta(days=7)  # text changes older than this start nothing
DEFAULT_PER_DAY = 3
MAX_PER_DAY = 24
TEXT = "text"  # the trigger of a rewrite after a text change
CORRECTION = "correction"  # ... after the athlete's correction
PUBLISH = "publish"  # the first insight after the Publish tick
TEXT_EVENT = "workout.texts_changed"
CORRECTION_EVENT = "insight.correction"
PUBLISH_EVENT = "workout.published"
# Versions that are a change of the athlete's texts (not a seed, not a lost sync version).
_KINDS = ("edit", "restore")
_SOURCES = ("vs", "strava", "garmin")


def per_day(rt) -> int:
    """Rewrites per workout per local day (``[coach_runner] insight_rewrites_per_day``)."""
    raw = (getattr(rt.settings, "coach_runner", None) or {}).get(
        "insight_rewrites_per_day", DEFAULT_PER_DAY
    )
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_PER_DAY
    return max(0, min(MAX_PER_DAY, raw))


def _when(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        found = raw
    elif raw:
        try:
            found = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    else:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _active(rt, now: datetime):
    """The runner settings when an insight may run now, else None."""
    from vibesynergy.coach_runner import runner, schedule

    cfg = schedule._active(rt)
    if cfg is None or cfg.tasks.get("insight", {}).get("on") is False:
        return None
    if runner.over_budget(rt, cfg, now):
        return None
    return cfg


def run_key(group: str, version: int) -> str:
    return f"{TEXT}-{group}-{version}"


def rewrites_today(rt, group: str, now: datetime | None = None) -> int:
    """Rewrites of this workout's insight started today (local): what counts for the limit."""
    from vibesynergy.coach_runner import runner, triggers
    from vibesynergy.store.schema import coach_run

    now = now or utc_now()
    start = triggers.day_start(rt, now.astimezone(runner.zone(rt)).date())
    with rt.engine.connect() as conn:
        return int(
            conn.scalar(
                select(func.count()).where(
                    coach_run.c.job == "insight",
                    coach_run.c.run_key.like(f"{TEXT}-{group}-%"),
                    coach_run.c.started_at >= start,
                )
            )
            or 0
        )


def _changes(rt, now: datetime, groups: set[str] | None = None) -> dict[str, list[Any]]:
    """Per workout (its group now), the text versions of the last ``LOOKBACK`` that are a
    change by the athlete or an outside platform, oldest first."""
    from vibesynergy.query import views, workout_texts
    from vibesynergy.store.schema import workout_text

    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(workout_text)
            .where(
                workout_text.c.at >= now - LOOKBACK,
                workout_text.c.kind.in_(_KINDS),
                workout_text.c.source.in_(_SOURCES),
                workout_text.c.field.in_(workout_texts.ALL_FIELDS),
            )
            .order_by(workout_text.c.id)
        ).all()
    resolved: dict[str, str | None] = {}
    out: dict[str, list[Any]] = {}
    for row in rows:
        if row.field == workout_texts.CORRECTION and not (row.text or "").strip():
            continue
        if row.group_id not in resolved:
            try:
                resolved[row.group_id] = views.resolve_activity(rt.engine, row.group_id)
            except (LookupError, ValueError, IndexError):
                resolved[row.group_id] = None
        group = resolved[row.group_id]
        if group is None or (groups is not None and group not in groups):
            continue
        out.setdefault(group, []).append(row)
    return out


def _state(rt, group: str, rows: list[Any], now: datetime, limit: int) -> dict[str, Any] | None:
    """What a workout's texts ask of its insight now, or None when nothing."""
    from vibesynergy.coach_runner import runner, schedule
    from vibesynergy.query import publication_insight, workout_texts

    try:
        insight = publication_insight.for_activity(rt.engine, group)
    except (LookupError, ValueError):
        insight = None
    if insight is None:
        return None  # nothing to rewrite: the first insight reads the texts anyway
    written = _when(insight["at"])
    after = [r for r in rows if written is None or _when(r.at) > written]  # type: ignore[operator]
    if not after:
        return None
    newest = after[-1]
    key = run_key(group, newest.id)
    corrected = newest.field == workout_texts.CORRECTION
    found: dict[str, Any] = {
        "group": group,
        "run_key": key,
        "trigger": CORRECTION if any(r.field == workout_texts.CORRECTION for r in after) else TEXT,
        "fields": sorted({r.field for r in after}),
        "changed_at": _when(newest.at),
        "replaces": insight["entry_id"],
        "ready_at": _when(newest.at) if corrected else _when(newest.at) + DEBOUNCE,  # type: ignore[operator]
        "seen": runner.get_run(rt, "insight", key) is not None,
        "busy": False,
        "limit": False,
    }
    if found["seen"]:
        return found
    with rt.engine.connect() as conn:
        found["busy"] = schedule.insight_busy(conn, [group])
    found["limit"] = rewrites_today(rt, group, now) >= limit
    return found


def check(rt, now: datetime | None = None, groups: set[str] | None = None) -> list[dict]:
    """The workouts whose insight should be (or is about to be) written again."""
    now = now or utc_now()
    limit = per_day(rt)
    out = []
    for group, rows in _changes(rt, now, groups).items():
        found = _state(rt, group, rows, now, limit)
        if found is not None:
            out.append(found)
    return out


def _item(rt, found: dict[str, Any]) -> dict[str, Any] | None:
    from vibesynergy.coach_runner import schedule

    event = CORRECTION_EVENT if found["trigger"] == CORRECTION else TEXT_EVENT
    return schedule.insight_item(
        rt,
        found["group"],
        found["run_key"],
        trigger=found["trigger"],
        event_type=event,
        redo=True,
        replaces=found["replaces"],
        extra={"changed": found["fields"]},
    )


def due(rt, cfg, now: datetime | None = None) -> list[dict[str, Any]]:
    """The rewrites to queue now (the scheduler, every tick; ``schedule.due_runs``)."""
    now = now or utc_now()
    if cfg.tasks.get("insight", {}).get("on") is False or per_day(rt) <= 0:
        return []
    try:
        found = check(rt, now)
    except Exception:
        log.exception("checking the insight rewrites failed")
        return []
    out = []
    for item in found:
        if item["seen"] or item["busy"] or item["limit"] or item["ready_at"] > now:
            continue
        queued = _item(rt, item)
        if queued is not None:
            out.append(queued)
    return out


def queue_now(rt, activity_id: str, now: datetime | None = None) -> int | None:
    """Queue the rewrite of one workout now when it is due (after a correction: at once).
    None when nothing is due, it waits, or the runner is off."""
    from vibesynergy.coach_runner import schedule
    from vibesynergy.query import views

    now = now or utc_now()
    if _active(rt, now) is None or per_day(rt) <= 0:
        return None
    group = views.resolve_activity(rt.engine, activity_id)
    for item in check(rt, now, {group}):
        if item["seen"] or item["busy"] or item["limit"] or item["ready_at"] > now:
            continue
        queued = _item(rt, item)
        if queued is not None:
            return schedule.enqueue_item(rt, queued)
    return None


def after_publish(rt, activity_id: str, now: datetime | None = None) -> int | None:
    """Right after VibeSynergy published a workout: queue its first insight when it has none
    and none is on its way. Once per workout (run key ``publish-<group>``). Never fails the
    publication."""
    from vibesynergy.coach_runner import runner, schedule
    from vibesynergy.query import publication_insight, views

    now = now or utc_now()
    try:
        if _active(rt, now) is None:
            return None
        group = views.resolve_activity(rt.engine, activity_id)
        if publication_insight.for_activity(rt.engine, group) is not None:
            return None
        key = f"{PUBLISH}-{group}"
        if runner.get_run(rt, "insight", key) is not None:
            return None
        with rt.engine.connect() as conn:
            if schedule.insight_busy(conn, [group]):
                return None
        item = schedule.insight_item(
            rt, group, key, trigger=PUBLISH, event_type=PUBLISH_EVENT, redo=False
        )
        return schedule.enqueue_item(rt, item) if item is not None else None
    except Exception:
        log.exception("queueing the insight after publishing %s failed", activity_id)
        return None


def status(rt, activity_id: str, now: datetime | None = None) -> dict[str, Any]:
    """For the workout page: whether the insight is being written or rewritten.

    ``state``: ``writing`` (a first insight is on its way), ``rewriting`` (queued or running),
    ``waiting`` (the texts changed; it starts at ``starts_at``), ``limit`` (today's rewrites are
    used up; it follows tomorrow), ``off`` (the texts changed, but the AI coach is not on) or
    None (also when the rewrites are switched off with ``per_day`` 0). ``fields`` are the texts that changed after the insight."""
    from vibesynergy.coach_runner import schedule
    from vibesynergy.query import publication_insight, views

    now = now or utc_now()
    group = views.resolve_activity(rt.engine, activity_id)
    out: dict[str, Any] = {"state": None, "per_day": per_day(rt)}
    with rt.engine.connect() as conn:
        busy = schedule.insight_busy(conn, [group])
    if busy:
        try:
            present = publication_insight.for_activity(rt.engine, group) is not None
        except (LookupError, ValueError):
            present = False
        return {**out, "state": "rewriting" if present else "writing"}
    found = next((f for f in check(rt, now, {group}) if not f["seen"]), None)
    if found is None:
        return out
    out["fields"] = found["fields"]
    if per_day(rt) <= 0:
        return out  # rewrites switched off in the config: nothing to say
    if _active(rt, now) is None:
        return {**out, "state": "off"}
    if found["limit"]:
        return {**out, "state": "limit"}
    return {**out, "state": "waiting", "starts_at": max(found["ready_at"], now).isoformat()}
