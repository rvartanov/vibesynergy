"""The workout page in the portal (#114, phase 2): texts, history and the Publish tick.

One read for the page (``page``) and the writes behind it. The texts and their history are
``workout_texts`` (phase 1); the tick is the existing guarded publication (``publication``
preview, then publish or unpublish with that preview's version) and, for new workouts,
the automatic run (``publication_auto``).

The page has two parts, kept apart all the way:

- **public**: the AI coach's public insight and the statistics block (what publishing puts
  in the description, read-only) and the athlete's own public text;
- **private**: the AI coach's comments on the workout (its assessments, read-only) and the
  athlete's own private text.

Nothing here is reachable from a share link (``portal/share.py`` lists the routes a shared
page may read, and these are not among them).

What the tick does now: publishing writes VibeSynergy's section (the insight, the statistics
and the optional weather and quote) into the Strava description, below the text that is
already there, and copies that description to Garmin when the Garmin copy is on. Without a
Strava copy it writes the section into the Garmin description. The name and the texts on
this page are not sent anywhere by it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from vibesynergy.query import annotations, publication, publication_auto, views, workout_texts
from vibesynergy.store.schema import activity_unified


def _coach_comments(rt, activity_id: str) -> list[dict[str, Any]]:
    """The AI coach's assessments of the workout, newest first: its private comments."""
    from vibesynergy.query import predictions

    try:
        entries = predictions.activity_assessments(rt.engine, activity_id)
    except ValueError:
        return []
    out = []
    for entry in entries:
        text = str(entry.get("text") or "").strip()
        if text:
            out.append({"at": entry.get("at"), "text": text})
    return out


def _stats(rt, activity_id: str, language: str) -> list[str]:
    """The statistics lines of the public section (local figures only, no provider call)."""
    try:
        block = annotations.preview(rt, activity_id, language=language)
    except (LookupError, ValueError):
        return []
    if block.get("state") != "preview":
        return []
    return [str(line) for line in block.get("stats") or [] if str(line).strip()]


def _start(rt, group: str) -> datetime | None:
    with rt.engine.connect() as conn:
        return conn.scalar(
            select(activity_unified.c.start_utc).where(activity_unified.c.group_id == group)
        )


def _destination(rt, group: str) -> tuple[str | None, str | None]:
    """Where the tick publishes: Strava (one copy), else Garmin; None when neither fits."""
    for destination in ("strava", "garmin"):
        try:
            return destination, publication._source_id(rt, group, destination)
        except publication.Conflict:
            continue
    return None, None


def publish_state(rt, activity_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The Publish tick of one workout.

    ``state``: ``published`` (a verified section is on the platform), ``automatic`` (not yet,
    but new workouts are public and the automatic run will publish it), ``off`` (only you),
    ``busy`` (a write or removal was interrupted: the tick retries it) or ``unavailable`` (no
    single Strava or Garmin copy to publish to). ``default`` is the setting for new workouts:
    ``public`` or ``only_me``."""
    group = views.resolve_activity(rt.engine, activity_id)
    destination, source_id = _destination(rt, group)
    default = "public" if publication_auto.enabled(rt) else "only_me"
    out: dict[str, Any] = {
        "destination": destination,
        "default": default,
        "mirror": destination == "strava" and _mirror_on(rt),
    }
    if destination is None or source_id is None:
        return {**out, "state": "unavailable", "checked": False}
    ledger = publication._ledger(rt, destination, source_id)
    if ledger and ledger["state"] == "verified":
        return {**out, "state": "published", "checked": True}
    if ledger and ledger["state"] in ("pending", "removing"):
        return {**out, "state": "busy", "checked": ledger["state"] == "pending"}
    start = _start(rt, group)
    if (
        destination == "strava"
        and start is not None
        and publication_auto.will_publish(rt, source_id, start, now=now)
    ):
        return {**out, "state": "automatic", "checked": True}
    return {**out, "state": "off", "checked": False}


def _mirror_on(rt) -> bool:
    from vibesynergy.query import publication_mirror

    return publication_mirror.enabled(rt)


def page(rt, activity_id: str, *, language: str = "en") -> dict[str, Any]:
    """Everything the workout page shows of the texts: current texts, history, the public
    and private parts and the Publish tick."""
    group = views.resolve_activity(rt.engine, activity_id)
    found = workout_texts.texts(rt.engine, group)
    return {
        "activity_id": group,
        "texts": {field: found[field] for field in workout_texts.FIELDS},
        "history": workout_texts.history(rt.engine, group),
        "public": {
            "insight": annotations.insight(rt, group),
            "stats": _stats(rt, group, language),
        },
        "private": {"coach": _coach_comments(rt, group)},
        "publish": publish_state(rt, group),
        "limits": {"name": workout_texts.MAX_NAME, "text": workout_texts.MAX_TEXT},
    }


def save_texts(
    rt,
    activity_id: str,
    *,
    name: str | None = None,
    public: str | None = None,
    private: str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Save what the athlete changed on the page (source ``vs``, author ``portal``)."""
    done = workout_texts.set_texts(
        rt.engine, activity_id, name=name, public=public, private=private, author="portal"
    )
    return {"changed": done["changed"], **page(rt, activity_id, language=language)}


def restore_text(rt, activity_id: str, version: int, *, language: str = "en") -> dict[str, Any]:
    """Make an old version current again: a new version that copies it."""
    done = workout_texts.restore(rt.engine, version, activity_id=activity_id, author="portal")
    return {"changed": done["changed"], **page(rt, activity_id, language=language)}


class NotPublishable(publication.Conflict):
    """The tick cannot publish this workout (yet); ``code`` says why, for the page."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def set_published(
    rt, activity_id: str, on: bool, *, language: str | None = None, client=None
) -> dict[str, Any]:
    """Tick or untick Publish: the same guarded path as the publish and unpublish tools
    (preview, then write with that preview's version, read back, ledger).

    Unticking also tells the automatic run to leave the workout alone, so a workout made
    "only me" stays that way. The text is in the language set for publication (as the
    automatic run writes it), not the language the page happens to be in."""
    language = language or publication_auto.language(rt)
    group = views.resolve_activity(rt.engine, activity_id)
    destination, source_id = _destination(rt, group)
    if destination is None or source_id is None:
        raise NotPublishable(
            "no_destination", "Connect a matching Garmin or Strava activity before publishing"
        )
    result: dict[str, Any] = {"state": "unchanged", "destination": destination}
    if on:
        item = publication.preview(rt, group, destination, language=language, client=client)
        if item["state"] == "blocked":
            raise NotPublishable("no_figures", "This activity has no figures to publish yet")
        if item["state"] == "undo_pending":
            # An interrupted removal: finish it first, then publish afresh.
            publication.unpublish(
                rt, group, destination, item["preview_version"], language=language, client=client
            )
            item = publication.preview(rt, group, destination, language=language, client=client)
        if item["state"] == "ready" and not item["already_published"]:
            result = publication.publish(
                rt, group, destination, item["preview_version"], language=language, client=client
            )
        elif item["state"] == "ready":
            result = {"state": "verified", "destination": destination}
    else:
        if destination == "strava":
            publication_auto.keep_private(rt, source_id, now=datetime.now(UTC))
        if publication._ledger(rt, destination, source_id):
            item = publication.preview(rt, group, destination, language=language, client=client)
            result = publication.unpublish(
                rt, group, destination, item["preview_version"], language=language, client=client
            )
    return {"result": result, "publish": publish_state(rt, group)}
