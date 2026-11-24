"""Agent events: VibeSynergy notes that something happened (docs/EVENTS.md).

VibeSynergy emits small versioned events, ``{"type", "version", "id", "occurred_at", "data"}``,
that carry ids and non-sensitive facts only. An AI coach reads the details and writes back
over MCP (``vibesynergy_activity_insights``, ``vibesynergy_coach_write``), so nothing here
depends on one AI provider. AI apps poll them (``vibesynergy_events`` over MCP, ``recent``);
code in this process gets them as internal subscribers.

``activity.ready`` is the first type. New activities are watched after every derive
(``observe``) and become ready once nothing merged into them for ``DEBOUNCE``, so the Garmin
and Strava copies end up in one event; a Strava-only activity waits up to ``WAIT_FOR_GARMIN``
for its Garmin copy while Garmin is connected. Activities that become ready together share one
event. Each watch keeps every member ``source:id`` it saw, so an activity is never emitted
twice, also not after a later merge or a restart. Only activities that start after the
feature first ran and within ``LOOKBACK`` count: switching it on never backfills.

``activity.published`` follows when the athlete makes the Strava copy public (Strava's
``private`` flag turns false; the webhook update re-fetches the activity, so the change shows
up after the next derive). That is the athlete saying the title, description and gear are
final and the duplicates are gone, so it is the trigger for the coach's public insight. It is
emitted once per activity, for watched activities first seen after the type existed
(``published_since``); a deleted Strava copy has no flag left and never emits.

The internal subscribers (the API coach runner's ``on_agent_event``) get every event in the
transaction that stores it. Each decides itself whether it is switched on.

The outgoing links (webhooks to routines in an AI app, with their outbox, signatures, quiet
hours and daily cap) were removed in #182. Their delivery history in ``agent_event_delivery``
stays as it was; ``retire_links`` drops the stored links and their secrets once.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from vibesynergy.core.secrets import SecretError, SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import (
    activity,
    activity_unified,
    agent_event,
    agent_event_delivery,
    agent_event_watch,
    audit_log,
)

log = logging.getLogger(__name__)

JOB = "agent_events"
VERSION = 1
ACTIVITY_READY = "activity.ready"
ACTIVITY_PUBLISHED = "activity.published"
COACH_RUN_MISSED = "coach.run_missed"
TYPES = (ACTIVITY_READY, ACTIVITY_PUBLISHED, COACH_RUN_MISSED)
# What the athlete has settled once the activity is public (the texts themselves never travel).
FINAL = ("title", "description", "gear")
# Reserved for later versions (docs/EVENTS.md): day.checkin, plan.changed.

DEBOUNCE = timedelta(minutes=5)
WAIT_FOR_GARMIN = timedelta(minutes=30)
LOOKBACK = timedelta(days=2)
KEEP = timedelta(days=30)
# The encrypted store keeps when events started (``since``). Until #182 it also held the
# outgoing links and their secrets (``subscriptions``).
STORE = "agent_events"
LINKS = "subscriptions"
LINKS_RETIRED = "links_removed_182"
# Delivery states that were still waiting when the links went.
OPEN = ("pending", "retry", "sending")

# --- internal subscribers (in-process, no HTTP) ---------------------------------------------

InternalSubscriber = Callable[[Any, Any, dict[str, Any]], Any]


def internal_subscribers() -> list[InternalSubscriber]:
    """In-process consumers of every event, called as ``fn(rt, conn, event)``."""
    from vibesynergy.coach_runner.schedule import on_agent_event

    return [on_agent_event]


def _notify_internal(rt, conn, event: dict[str, Any]) -> None:
    """A subscriber's failure is logged; the event itself is always stored."""
    for subscriber in internal_subscribers():
        try:
            subscriber(rt, conn, event)
        except Exception:
            log.exception("an internal agent-event subscriber failed")


# --- the encrypted store: when events started, and the retired links -----------------------


def _store(rt) -> SecretStore:
    return SecretStore(rt.settings.secrets_dir, STORE)


def _retired(rt) -> bool:
    return LINKS_RETIRED in ((_store(rt).get("state") or {}).get("migrated") or [])


def retire_links(rt, *, now: datetime | None = None) -> int:
    """Once, after the upgrade to #182: drop the stored outgoing links with their secrets,
    and close the deliveries that were still waiting. Returns how many links were dropped.

    The delivery history itself stays. Each dropped link is in the audit log (name only).
    """
    store = _store(rt)
    if _retired(rt):
        return 0
    now = now or utc_now()
    with store.lock(LINKS):
        try:
            links = dict(store.get(LINKS) or {})
        except SecretError:  # unreadable: it goes all the same, only unnamed
            links = {}
        store.delete(LINKS)
    with rt.engine.begin() as conn:
        conn.execute(
            update(agent_event_delivery)
            .where(agent_event_delivery.c.state.in_(OPEN))
            .values(state="skipped", error="outgoing links removed")
        )
        for sub_id, sub in sorted(links.items()):
            conn.execute(
                audit_log.insert().values(
                    at=now,
                    actor="vibesynergy",
                    action="events.subscription.delete",
                    target=f"subscription:{sub_id}",
                    outcome="ok",
                    detail={"name": (sub or {}).get("name"), "reason": "links removed (#182)"},
                )
            )
    with store.lock("state"):
        state = store.get("state") or {}
        state["migrated"] = [*(state.get("migrated") or []), LINKS_RETIRED]
        store.set("state", state)
    if links:
        log.warning(
            "the outgoing coach links are gone (#182): %d stored link(s) and their secrets "
            "were removed; events stay readable over MCP",
            len(links),
        )
    return len(links)


# --- watching and emitting -----------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def since(rt, now: datetime | None = None, *, key: str = "since") -> datetime:
    """When events started: the first time this ran. Nothing before it is emitted.

    ``key="published_since"`` is the same for ``activity.published``, which came later.
    """
    store = _store(rt)
    state = store.get("state") or {}
    if state.get(key):
        return _aware(datetime.fromisoformat(state[key]))
    with store.lock("state"):
        state = store.get("state") or {}
        if not state.get(key):
            state[key] = (now or utc_now()).isoformat()
            store.set("state", state)
    return _aware(datetime.fromisoformat(state[key]))


def _garmin_connected(rt) -> bool:
    try:
        return "garmin" in rt.registry.enabled_ids() and rt.registry.get("garmin").configured()
    except Exception:
        return False


def observe(rt, *, now: datetime | None = None) -> int:
    """Note new activities after a derive and push their debounce on every merge.

    Returns how many activities are being watched. Safe to call any number of times.
    """
    now = now or utc_now()
    floor = max(since(rt, now), now - LOOKBACK)
    with rt.engine.begin() as conn:
        rows = (
            conn.execute(
                select(activity_unified.c.group_id, activity_unified.c.sources).where(
                    activity_unified.c.start_utc >= floor
                )
            )
            .mappings()
            .all()
        )
        watches = [
            dict(w)
            for w in conn.execute(
                select(agent_event_watch).where(agent_event_watch.c.first_seen_at >= now - KEEP)
            ).mappings()
        ]
        for row in rows:
            refs = {str(r) for r in row["sources"] or []}
            if not refs:
                continue
            matches = [w for w in watches if refs & set(w["refs"])]
            if not matches:
                values = {
                    "group_id": row["group_id"],
                    "refs": sorted(refs),
                    "first_seen_at": now,
                    "due_at": now + DEBOUNCE,
                    "event_id": None,
                }
                new_id = conn.execute(
                    agent_event_watch.insert().values(**values)
                ).inserted_primary_key[0]
                watches.append({"id": new_id, **values})
                continue
            emitted = [w for w in matches if w["event_id"]]
            keep = emitted[0] if emitted else min(matches, key=lambda w: w["id"])
            union = set(refs)
            for w in matches:
                union |= set(w["refs"])
            changed = union != set(keep["refs"]) or len(matches) > 1
            if not changed and keep["group_id"] == row["group_id"]:
                continue
            values = {"refs": sorted(union), "group_id": row["group_id"]}
            if not keep["event_id"] and changed:
                values["due_at"] = max(_aware(keep["due_at"]), now + DEBOUNCE)
            if len(matches) > 1:
                values["first_seen_at"] = min(_aware(w["first_seen_at"]) for w in matches)
            conn.execute(
                update(agent_event_watch)
                .where(agent_event_watch.c.id == keep["id"])
                .values(**values)
            )
            keep.update(values)
            for w in matches:
                if w is not keep:
                    conn.execute(delete(agent_event_watch).where(agent_event_watch.c.id == w["id"]))
                    watches.remove(w)
    return len(watches)


def _ready_watches(rt, conn, now: datetime) -> list[dict[str, Any]]:
    garmin = _garmin_connected(rt)
    out = []
    for w in conn.execute(
        select(agent_event_watch).where(
            agent_event_watch.c.event_id.is_(None), agent_event_watch.c.due_at <= now
        )
    ).mappings():
        refs = list(w["refs"])
        strava_only = not any(r.startswith(("garmin:", "garmin_export:")) for r in refs)
        if garmin and strava_only and _aware(w["first_seen_at"]) + WAIT_FOR_GARMIN > now:
            continue
        out.append(dict(w))
    return out


def _new_id(prefix: str = "evt") -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _activity_facts(rt, conn, group_id: str) -> dict[str, Any] | None:
    from vibesynergy.query import publication_insight

    row = (
        conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.local_date,
                activity_unified.c.sport,
                activity_unified.c.sources,
            ).where(activity_unified.c.group_id == group_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    try:
        insight = publication_insight.for_activity(rt.engine, group_id)
    except ValueError:
        insight = None
    return {
        "id": row["group_id"],
        "sources": sorted(str(r) for r in row["sources"] or []),
        "local_date": row["local_date"].isoformat(),
        "sport": row["sport"],
        "public_insight_missing": insight is None,
    }


def _current_group(conn, w: dict[str, Any]) -> str | None:
    """The watch's activity now: its group, or the group that took any member it had."""
    if conn.execute(
        select(activity_unified.c.group_id).where(activity_unified.c.group_id == w["group_id"])
    ).first():
        return w["group_id"]
    current = conn.execute(select(activity_unified.c.group_id, activity_unified.c.sources)).all()
    return next((g for g, s in current if set(map(str, s or [])) & set(w["refs"])), None)


def _watch_facts(rt, conn, w: dict[str, Any]) -> dict[str, Any] | None:
    group = _current_group(conn, w)
    return _activity_facts(rt, conn, group) if group else None


def emit_ready(rt, *, now: datetime | None = None) -> str | None:
    """Turn every debounced activity into one ``activity.ready`` event and hand it to the
    internal subscribers. Returns the event id, or None when nothing was ready."""
    now = now or utc_now()
    with rt.engine.begin() as conn:
        ready = _ready_watches(rt, conn, now)
        if not ready:
            return None
        activities = []
        for w in ready:
            facts = _watch_facts(rt, conn, w)
            if facts is not None:
                activities.append(facts)
        event_id = _new_id()
        for w in ready:
            conn.execute(
                update(agent_event_watch)
                .where(agent_event_watch.c.id == w["id"])
                .values(event_id=event_id)
            )
        if not activities:  # removed meanwhile: nothing to tell
            return None
        conn.execute(
            agent_event.insert().values(
                id=event_id,
                type=ACTIVITY_READY,
                version=VERSION,
                occurred_at=now,
                data={"activities": activities},
            )
        )
        _notify_internal(
            rt,
            conn,
            {"type": ACTIVITY_READY, "id": event_id, "data": {"activities": activities}},
        )
    return event_id


def _public_strava(conn, refs: list[str]) -> list[str]:
    """The Strava members (``strava:<id>``) whose copy is public now. A deleted copy has no
    row, so it never counts."""
    ids = [r.split(":", 1)[1] for r in refs if r.startswith("strava:")]
    if not ids:
        return []
    rows = conn.execute(
        select(activity.c.source_id, activity.c.extra).where(
            activity.c.source == "strava", activity.c.source_id.in_(ids)
        )
    ).all()
    return sorted(f"strava:{sid}" for sid, extra in rows if (extra or {}).get("private") is False)


def _published_refs(conn, now: datetime) -> set[str]:
    """Every member named by an ``activity.published`` event still kept."""
    out: set[str] = set()
    for (data,) in conn.execute(
        select(agent_event.c.data).where(
            agent_event.c.type == ACTIVITY_PUBLISHED, agent_event.c.occurred_at >= now - KEEP
        )
    ):
        for item in (data or {}).get("activities") or []:
            out |= set(item.get("sources") or [])
    return out


def _published_watches(rt, conn, now: datetime) -> list[tuple[str, list[str]]]:
    """(group id, public Strava refs) for settled activities whose Strava copy turned public
    and that were never announced as published."""
    floor = max(since(rt, now, key="published_since"), now - LOOKBACK)
    done = _published_refs(conn, now)
    out: dict[str, list[str]] = {}
    for w in conn.execute(
        select(agent_event_watch).where(
            agent_event_watch.c.first_seen_at >= floor, agent_event_watch.c.due_at <= now
        )
    ).mappings():
        group = _current_group(conn, dict(w))
        if group is None or group in out:
            continue
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
        )
        refs = sorted(str(r) for r in sources or [])
        public = _public_strava(conn, refs)
        if public and not (set(refs) | set(w["refs"])) & done:
            out[group] = public
    return list(out.items())


def emit_published(rt, *, now: datetime | None = None) -> str | None:
    """One ``activity.published`` event for every activity whose Strava copy became public
    since the last run. Returns the event id, or None when nothing turned public."""
    now = now or utc_now()
    with rt.engine.begin() as conn:
        found = _published_watches(rt, conn, now)
        activities = []
        for group, public in found:
            facts = _activity_facts(rt, conn, group)
            if facts is not None:
                activities.append({**facts, "public": public})
        if not activities:
            return None
        event_id = _new_id()
        conn.execute(
            agent_event.insert().values(
                id=event_id,
                type=ACTIVITY_PUBLISHED,
                version=VERSION,
                occurred_at=now,
                data={"activities": activities, "final": list(FINAL)},
            )
        )
        _notify_internal(
            rt,
            conn,
            {
                "type": ACTIVITY_PUBLISHED,
                "id": event_id,
                "data": {"activities": activities, "final": list(FINAL)},
            },
        )
    return event_id


def _missed_unsent(rt, conn, now: datetime) -> bool:
    """A missed coach run with no event yet ( catch-up), so ``run`` is not left waiting
    for some other event. Cheap: a few reads of the coach log."""
    from vibesynergy.query import coach_tasks

    missing = {
        (task["id"], day) for task in coach_tasks.status_for(rt, now) for day in task["missing"]
    }
    if not missing:
        return False
    sent = {
        (row.data.get("task"), row.data.get("date"))
        for row in conn.execute(
            select(agent_event.c.data).where(
                agent_event.c.type == COACH_RUN_MISSED,
                agent_event.c.occurred_at >= now - KEEP,
            )
        )
    }
    return bool(missing - sent)


def emit_missed(rt, *, now: datetime | None = None) -> list[str]:
    """One ``coach.run_missed`` event per coach routine that did not run, once per
    task and day, so the AI coach inside VibeSynergy can catch it up (``_catch_up``).
    Returns the new event ids."""
    from vibesynergy.query import coach_tasks

    now = now or utc_now()
    try:
        tasks = coach_tasks.status_for(rt, now)
    except Exception:
        log.exception("agent events: the coach task register could not be read")
        return []
    missing = [(task, day) for task in tasks for day in task["missing"]]
    if not missing:
        return []
    out = []
    with rt.engine.begin() as conn:
        sent = {
            (row.data.get("task"), row.data.get("date"))
            for row in conn.execute(
                select(agent_event.c.data).where(
                    agent_event.c.type == COACH_RUN_MISSED,
                    agent_event.c.occurred_at >= now - KEEP,
                )
            )
        }
        for task, day in missing:
            if (task["id"], day) in sent:
                continue
            data = {
                "task": task["id"],
                "date": day,
                "entry_type": task["entry_type"],
                "prompt": task["prompt"],
                "idempotency_key": f"{task['key_prefix']}{day}" if task["key_prefix"] else None,
            }
            event_id = _new_id()
            conn.execute(
                agent_event.insert().values(
                    id=event_id, type=COACH_RUN_MISSED, version=VERSION, occurred_at=now, data=data
                )
            )
            _notify_internal(rt, conn, {"type": COACH_RUN_MISSED, "id": event_id, "data": data})
            out.append(event_id)
    return out


def envelope(row) -> dict[str, Any]:
    occurred = _aware(row["occurred_at"]).astimezone(UTC)
    return {
        "type": row["type"],
        "version": row["version"],
        "id": row["id"],
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "data": row["data"],
    }


def recent(
    rt, *, since_at: str | None = None, types: list[str] | None = None, limit: int = 50
) -> dict[str, Any]:
    """Events after ``since_at`` (ISO time or an event's occurred_at; default the last 24
    hours), oldest first, for agents that poll. ``next_since`` continues the list."""
    if not 1 <= limit <= 200:
        raise ValueError("limit runs from 1 to 200")
    wanted = list(types or TYPES)
    unknown = set(wanted) - set(TYPES)
    if unknown:
        raise ValueError(
            f"unknown event type: {', '.join(sorted(unknown))}; known: {', '.join(TYPES)}"
        )
    if since_at:
        try:
            start = _aware(datetime.fromisoformat(since_at.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError("since is an ISO time such as 2026-10-31T08:00:00Z") from exc
    else:
        start = utc_now() - timedelta(hours=24)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(agent_event)
                .where(agent_event.c.occurred_at > start, agent_event.c.type.in_(wanted))
                .order_by(agent_event.c.occurred_at, agent_event.c.id)
                .limit(limit)
            )
            .mappings()
            .all()
        )
    events = [envelope(row) for row in rows]
    return {
        "events": events,
        "next_since": events[-1]["occurred_at"] if events else (since_at or None),
        "types": list(TYPES),
    }


# --- the scheduled job ---------------------------------------------------------------------


def run(rt, *, now: datetime | None = None) -> dict[str, Any]:
    """The scheduled job: watch, emit what is ready, tidy up."""
    now = now or utc_now()
    retire_links(rt, now=now)
    observe(rt, now=now)
    event_id = emit_ready(rt, now=now)
    published = emit_published(rt, now=now)
    missed = emit_missed(rt, now=now)
    _prune(rt, now)
    return {"event": event_id, "published": published, "missed": missed}


def _prune(rt, now: datetime) -> None:
    """Old watches and events go after ``KEEP``. An event the delivery history names stays
    with it: that history is kept as it was when the links went (#182)."""
    with rt.engine.begin() as conn:
        conn.execute(
            delete(agent_event_watch).where(agent_event_watch.c.first_seen_at < now - KEEP)
        )
        conn.execute(
            delete(agent_event).where(
                agent_event.c.occurred_at < now - KEEP,
                agent_event.c.id.not_in(select(agent_event_delivery.c.event_id)),
            )
        )


def due(rt, *, now: datetime | None = None) -> bool:
    """Asked on every scheduler tick; a failure here must not stop the other syncs."""
    now = now or utc_now()
    try:
        if not _retired(rt):
            return True
        with rt.engine.connect() as conn:
            waiting = conn.execute(
                select(func.count())
                .select_from(agent_event_watch)
                .where(agent_event_watch.c.event_id.is_(None), agent_event_watch.c.due_at <= now)
            ).scalar()
            if waiting and _ready_watches(rt, conn, now):
                return True
            if _published_watches(rt, conn, now):
                return True
            return _missed_unsent(rt, conn, now)
    except Exception:
        log.exception("checking for agent events failed")
        return False


def after_derive(rt) -> None:
    """Called after every derive; never lets a problem here fail the sync."""
    try:
        observe(rt)
    except Exception:
        log.exception("watching new activities for agent events failed")
