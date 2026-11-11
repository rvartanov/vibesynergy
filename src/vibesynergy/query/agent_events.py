"""Agent events: tell outside coach agents that something happened (docs/EVENTS.md).

VibeSynergy emits small versioned events, ``{"type", "version", "id", "occurred_at", "data"}``,
that carry ids and non-sensitive facts only. An agent reads the details and writes back over
MCP (``vibesynergy_activity_insights``, ``vibesynergy_coach_write``), so nothing here depends on one
AI provider. Agents either poll (``vibesynergy_events`` over MCP, ``recent``) or subscribe a
webhook in Settings → Sync between sources.

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

Delivery is an outbox (``agent_event_delivery``): one row per event and subscription, written
in the same transaction as the event. The scheduled job sends due rows. Before each send the
row is marked ``sending``, so a crash mid-send leaves ``unknown`` and is never sent again
(Standard Webhooks consumers can also dedupe on ``webhook-id``). 429, 5xx and network errors
retry with exponential backoff, at most ``MAX_TRIES`` times; other 4xx stop. Each subscription
has a daily cap and quiet hours (local time); deliveries wait for the end of the quiet hours
or the next day, and expire after ``EXPIRE``. With "only without a coach insight" (the
default), an ``activity.ready`` whose activities all got an insight meanwhile is skipped.
Every send is audit-logged. Secrets live encrypted in the ``agent_events`` secret store and
never leave it: not in logs, the audit log, errors or API responses.

Besides webhook links there are internal subscribers: code in this process (the API coach
runner's ``on_agent_event``) that gets every event in the transaction that stores it, with no
HTTP, outbox or quiet hours. Each decides itself whether it is switched on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, update

from vibesynergy.core.secrets import SecretStore
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
TEST = "vibesynergy.test"
TYPES = (ACTIVITY_READY, ACTIVITY_PUBLISHED, COACH_RUN_MISSED)
# A new link listens for the public Strava activity: the coach insight's trigger (1 Nov 2026).
DEFAULT_EVENTS = (ACTIVITY_PUBLISHED,)
# What the athlete has settled once the activity is public (the texts themselves never travel).
FINAL = ("title", "description", "gear")
# Reserved for later versions (docs/EVENTS.md): day.checkin, plan.changed.

DEBOUNCE = timedelta(minutes=5)
WAIT_FOR_GARMIN = timedelta(minutes=30)
LOOKBACK = timedelta(days=2)
KEEP = timedelta(days=30)
EXPIRE = timedelta(hours=18)
MAX_TRIES = 4
BACKOFF = timedelta(minutes=5)  # 5, 10, 20 minutes
MAX_BACKOFF = timedelta(hours=2)
STALE_SENDING = timedelta(minutes=10)
TIMEOUT_S = 20
DAILY_CAP = 10
QUIET_START = "22:30"
QUIET_END = "06:30"
MAX_SUBSCRIPTIONS = 10
AUTH_MODES = ("webhook", "bearer", "claude_routine")
CLAUDE_BETA = "experimental-cc-routine-2026-04-01"
ANTHROPIC_VERSION = "2023-06-01"  # required next to the beta header, else 400
CLAUDE_URL = re.compile(
    r"^https://api\.anthropic\.com/v1/claude_code/routines/[A-Za-z0-9_-]+/fire$"
)
CLAUDE_ID = re.compile(r"^trig_[A-Za-z0-9]+$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OPEN = ("pending", "retry")
STORE = "agent_events"


class SubscriptionError(ValueError):
    """A subscription was not saved."""


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    text: str


Exchange = Callable[[str, dict[str, str], bytes], Reply]


def _requests_exchange(url: str, headers: dict[str, str], body: bytes) -> Reply:
    import requests

    response = requests.post(
        url, data=body, headers=headers, timeout=TIMEOUT_S, allow_redirects=False
    )
    return Reply(response.status_code, dict(response.headers), response.text[:4000])


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


# --- subscriptions (encrypted secret store) ------------------------------------------------


def _store(rt) -> SecretStore:
    return SecretStore(rt.settings.secrets_dir, STORE)


def _subscriptions(rt) -> dict[str, dict[str, Any]]:
    return dict(_store(rt).get("subscriptions") or {})


def public(sub: dict[str, Any]) -> dict[str, Any]:
    """A subscription as the page and API see it: never its secret."""
    out = {key: value for key, value in sub.items() if key != "secret"}
    out["has_secret"] = bool(sub.get("secret"))
    return out


def subscriptions(rt) -> list[dict[str, Any]]:
    subs = sorted(_subscriptions(rt).values(), key=lambda s: s.get("created_at") or "")
    return [public(sub) for sub in subs]


def normalise_url(auth: str, url: str) -> str:
    url = (url or "").strip()
    if auth == "claude_routine":
        if CLAUDE_ID.match(url):
            url = f"https://api.anthropic.com/v1/claude_code/routines/{url}/fire"
        if not CLAUDE_URL.match(url):
            raise SubscriptionError(
                "Enter the routine's API address "
                "(https://api.anthropic.com/v1/claude_code/routines/<id>/fire) or its id."
            )
        return url
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    secure = parts.scheme == "https" and bool(parts.hostname)
    if not secure and not (parts.scheme == "http" and local):
        raise SubscriptionError("Enter an https:// address (http only for this computer).")
    if parts.username or parts.password:
        raise SubscriptionError("Put credentials in the secret field, not in the address.")
    if len(url) > 500:
        raise SubscriptionError("The address is too long.")
    return url


def _clean(values: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(current or {})
    for key in (
        "name",
        "url",
        "events",
        "auth",
        "enabled",
        "daily_cap",
        "quiet_start",
        "quiet_end",
        "only_missing_insight",
    ):
        if key in values and values[key] is not None:
            base[key] = values[key]
    base.setdefault("events", list(DEFAULT_EVENTS))
    base.setdefault("enabled", False)
    base.setdefault("daily_cap", DAILY_CAP)
    base.setdefault("quiet_start", QUIET_START)
    base.setdefault("quiet_end", QUIET_END)
    base.setdefault("only_missing_insight", True)
    name = " ".join(str(base.get("name") or "").split())
    if not name or len(name) > 60:
        raise SubscriptionError("Give the link a name of at most 60 characters.")
    base["name"] = name
    auth = base.get("auth")
    if auth not in AUTH_MODES:
        raise SubscriptionError("Choose how VibeSynergy signs in to the address.")
    base["url"] = normalise_url(auth, str(base.get("url") or ""))
    events = base["events"]
    if not isinstance(events, list) or not events or not set(events) <= set(TYPES):
        raise SubscriptionError("Choose at least one event.")
    base["events"] = [t for t in TYPES if t in events]
    try:
        cap = int(base["daily_cap"])
    except (TypeError, ValueError):
        raise SubscriptionError("The daily limit is a whole number from 1 to 50.") from None
    if not 1 <= cap <= 50:
        raise SubscriptionError("The daily limit is a whole number from 1 to 50.")
    base["daily_cap"] = cap
    for key in ("quiet_start", "quiet_end"):
        if not isinstance(base[key], str) or not _HHMM.match(base[key]):
            raise SubscriptionError("Quiet hours are times like 22:30.")
    base["enabled"] = base["enabled"] is True
    base["only_missing_insight"] = base["only_missing_insight"] is True
    return base


def _audit_change(
    rt, action: str, sub: dict[str, Any], fields: list[str], *, actor: str = "portal"
) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor=actor,
                action=action,
                target=f"subscription:{sub['id']}",
                outcome="ok",
                detail={"name": sub.get("name"), "fields": sorted(fields)},
            )
        )


def save_subscription(
    rt, values: dict[str, Any], *, sub_id: str | None = None, secret: str | None = None
) -> dict[str, Any]:
    """Create (no ``sub_id``) or change one subscription. ``secret`` replaces the stored
    one when given; it is never returned. Enabling needs a secret."""
    store = _store(rt)
    with store.lock("subscriptions"):
        subs = _subscriptions(rt)
        current = subs.get(sub_id) if sub_id else None
        if sub_id and current is None:
            raise SubscriptionError("This link no longer exists.")
        if not sub_id and len(subs) >= MAX_SUBSCRIPTIONS:
            raise SubscriptionError(f"At most {MAX_SUBSCRIPTIONS} links.")
        sub = _clean(values, current)
        if secret is not None:
            secret = secret.strip()
            if not secret or len(secret) > 2000 or any(c.isspace() for c in secret):
                raise SubscriptionError("The secret is one word without spaces.")
            sub["secret"] = secret
        if sub["enabled"] and not sub.get("secret"):
            raise SubscriptionError("Save the secret or token first, then switch it on.")
        now = utc_now().isoformat()
        if current is None:
            sub["id"] = f"sub_{secrets.token_hex(6)}"
            sub["created_at"] = now
        if sub["enabled"] and not (current or {}).get("enabled"):
            sub["enabled_at"] = now  # only events from now on reach it
        sub["updated_at"] = now
        subs[sub["id"]] = sub
        store.set("subscriptions", subs)
    changed = [
        k for k in values if values.get(k) is not None and (current or {}).get(k) != sub.get(k)
    ]
    if secret is not None:
        changed.append("secret")
    _audit_change(
        rt,
        "events.subscription.create" if current is None else "events.subscription.update",
        sub,
        changed,
    )
    return public(sub)


def delete_subscription(rt, sub_id: str) -> None:
    store = _store(rt)
    with store.lock("subscriptions"):
        subs = _subscriptions(rt)
        sub = subs.pop(sub_id, None)
        if sub is None:
            raise SubscriptionError("This link no longer exists.")
        store.set("subscriptions", subs)
    with rt.engine.begin() as conn:
        conn.execute(
            update(agent_event_delivery)
            .where(
                agent_event_delivery.c.subscription_id == sub_id,
                agent_event_delivery.c.state.in_(OPEN),
            )
            .values(state="skipped", error="link removed")
        )
    _audit_change(rt, "events.subscription.delete", sub, [])


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


MIGRATION = "published_v1"


def migrate(rt, *, now: datetime | None = None) -> list[str]:
    """Once: a Claude routine link that only wanted ``activity.ready`` for the coach insight
    now listens for ``activity.published``. Returns the changed ids.

    Runs from code, never by hand: links live in the encrypted store, not the database. A
    link set back to ``activity.ready`` later stays that way.
    """
    store = _store(rt)
    if MIGRATION in ((store.get("state") or {}).get("migrated") or []):
        return []
    stamp = (now or utc_now()).isoformat()
    changed = []
    with store.lock("subscriptions"):
        subs = _subscriptions(rt)
        for sub in subs.values():
            if (
                sub.get("auth") == "claude_routine"
                and sub.get("events") == [ACTIVITY_READY]
                and sub.get("only_missing_insight", True)
            ):
                sub["events"] = [ACTIVITY_PUBLISHED]
                sub["updated_at"] = stamp
                changed.append(sub)
        if changed:
            store.set("subscriptions", subs)
    with store.lock("state"):
        state = store.get("state") or {}
        state["migrated"] = [*(state.get("migrated") or []), MIGRATION]
        store.set("state", state)
    for sub in changed:
        _audit_change(rt, "events.subscription.update", sub, ["events"], actor="vibesynergy")
    return [sub["id"] for sub in changed]


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


def _wanting(rt, event_type: str) -> list[dict[str, Any]]:
    return [
        sub
        for sub in _subscriptions(rt).values()
        if sub.get("enabled") and sub.get("secret") and event_type in (sub.get("events") or [])
    ]


def emit_ready(rt, *, now: datetime | None = None) -> str | None:
    """Turn every debounced activity into one ``activity.ready`` event and queue it for the
    subscriptions that want it. Returns the event id, or None when nothing was ready."""
    now = now or utc_now()
    subs = _wanting(rt, ACTIVITY_READY)
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
        for sub in subs:
            conn.execute(
                agent_event_delivery.insert().values(
                    event_id=event_id,
                    subscription_id=sub["id"],
                    state="pending",
                    attempts=0,
                    created_at=now,
                    next_at=now,
                )
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
    subs = _wanting(rt, ACTIVITY_PUBLISHED)
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
        for sub in subs:
            conn.execute(
                agent_event_delivery.insert().values(
                    event_id=event_id,
                    subscription_id=sub["id"],
                    state="pending",
                    attempts=0,
                    created_at=now,
                    next_at=now,
                )
            )
    return event_id


def _missed_unsent(rt, conn, now: datetime) -> bool:
    """A missed coach run with no event yet ( catch-up), so ``run`` is not left waiting
    for some other event. Cheap: a few reads of the coach log."""
    from vibesynergy.query import coach_tasks

    missing = {
        (task["id"], day)
        for task in coach_tasks.status(rt.engine, now, rt.settings.timezone)
        for day in task["missing"]
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
    task and day, so a webhook link can start the routine without anyone noticing first.
    Returns the new event ids."""
    from vibesynergy.query import coach_tasks

    now = now or utc_now()
    subs = _wanting(rt, COACH_RUN_MISSED)
    try:
        tasks = coach_tasks.status(rt.engine, now, rt.settings.timezone)
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
            for sub in subs:
                conn.execute(
                    agent_event_delivery.insert().values(
                        event_id=event_id,
                        subscription_id=sub["id"],
                        state="pending",
                        attempts=0,
                        created_at=now,
                        next_at=now,
                    )
                )
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


# --- delivery ------------------------------------------------------------------------------


def _local(rt, value: datetime) -> datetime:
    return value.astimezone(ZoneInfo(rt.settings.timezone))


def quiet_until(rt, sub: dict[str, Any], now: datetime) -> datetime | None:
    """The end of the quiet hours when ``now`` falls inside them, else None."""
    start = time.fromisoformat(sub.get("quiet_start") or QUIET_START)
    end = time.fromisoformat(sub.get("quiet_end") or QUIET_END)
    if start == end:
        return None
    local = _local(rt, now)
    clock = local.time()
    inside = start <= clock or clock < end if start > end else start <= clock < end
    if not inside:
        return None
    day = local.date() if clock < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=local.tzinfo).astimezone(UTC)


def _day_start(rt, now: datetime) -> datetime:
    local = _local(rt, now)
    return datetime.combine(local.date(), time(0), tzinfo=local.tzinfo).astimezone(UTC)


def _sent_today(conn, rt, sub_id: str, now: datetime) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(agent_event_delivery)
            .where(
                agent_event_delivery.c.subscription_id == sub_id,
                agent_event_delivery.c.attempted_at >= _day_start(rt, now),
            )
        ).scalar()
        or 0
    )


def _signature(secret: str, msg_id: str, stamp: str, body: bytes) -> str:
    """Standard Webhooks v1: HMAC-SHA256 over ``id.timestamp.body``, base64."""
    key = (
        base64.b64decode(secret.removeprefix("whsec_"))
        if secret.startswith("whsec_")
        else secret.encode()
    )
    mac = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def claude_text(event: dict[str, Any], activity_ids: list[str] | None = None) -> str:
    """The routine's run context: ids only, nothing it should follow as instructions."""
    parts = [f"event={event['type']}", f"event_id={event['id']}"]
    if activity_ids:
        parts.append("activity_ids=" + ",".join(activity_ids))
    if event["type"] == COACH_RUN_MISSED:
        data = event.get("data") or {}
        parts += [f"task={data.get('task')}", f"date={data.get('date')}"]
    return " ".join(parts)


def request_for(
    sub: dict[str, Any], event: dict[str, Any], *, now: datetime, activity_ids: list[str] | None
) -> tuple[dict[str, str], bytes]:
    """Headers and body for one delivery. The only Claude-specific code is this adapter."""
    stamp = str(int(now.timestamp()))
    secret = sub.get("secret") or ""
    headers = {"Content-Type": "application/json", "User-Agent": "VibeSynergy-Events/1"}
    if sub["auth"] == "claude_routine":
        body = json.dumps({"text": claude_text(event, activity_ids)}).encode()
        headers["Authorization"] = f"Bearer {secret}"
        headers["anthropic-beta"] = CLAUDE_BETA
        headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers, body
    body = json.dumps(event, separators=(",", ":")).encode()
    headers["webhook-id"] = event["id"]
    headers["webhook-timestamp"] = stamp
    if sub["auth"] == "webhook":
        headers["webhook-signature"] = _signature(secret, event["id"], stamp, body)
    else:
        headers["Authorization"] = f"Bearer {secret}"
    return headers, body


def _scrub(text: str, sub: dict[str, Any]) -> str:
    secret = sub.get("secret")
    if secret:
        text = text.replace(secret, "***")
    return re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)[:300]


def _link(sub: dict[str, Any], reply: Reply) -> str | None:
    if sub["auth"] != "claude_routine":
        return None
    try:
        url = json.loads(reply.text).get("claude_code_session_url")
    except (ValueError, AttributeError):
        return None
    if isinstance(url, str) and url.startswith("https://claude.ai/") and len(url) < 300:
        return url
    return None


def _retry_after(reply: Reply | None, now: datetime) -> datetime | None:
    if reply is None:
        return None
    raw = {k.lower(): v for k, v in reply.headers.items()}.get("retry-after")
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return now + min(timedelta(seconds=max(seconds, 0)), MAX_BACKOFF)


def _audit_send(rt, sub, event, outcome: str, detail: dict[str, Any]) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="vibesynergy",
                action="events.delivery",
                target=f"subscription:{sub['id']}",
                outcome=outcome,
                detail={
                    "event_id": event["id"],
                    "type": event["type"],
                    "subscription": sub.get("name"),
                    "auth": sub.get("auth"),
                    **detail,
                },
            )
        )


def _activity_ids(rt, sub, event) -> list[str] | None:
    """Which activities to name; None when an activity event has nothing left to do."""
    if event["type"] not in (ACTIVITY_READY, ACTIVITY_PUBLISHED):
        return []
    from vibesynergy.query import publication_insight

    ids = [a["id"] for a in event["data"].get("activities") or []]
    if not sub.get("only_missing_insight", True):
        return ids
    missing = []
    for activity_id in ids:
        try:
            if publication_insight.for_activity(rt.engine, activity_id) is None:
                missing.append(activity_id)
        except ValueError:
            continue  # the activity is gone
    return missing or None


def _send(rt, sub, delivery, event, now: datetime, http: Exchange) -> str:
    """One attempt; the delivery row is already marked ``sending``. Returns the new state."""
    ids = _activity_ids(rt, sub, event)
    attempts = int(delivery["attempts"])
    values: dict[str, Any]
    if ids is None:
        values = {"state": "skipped", "error": "every activity has a coach insight"}
        state = "skipped"
        _finish(rt, delivery, values)
        return state
    headers, body = request_for(sub, event, now=now, activity_ids=ids)
    reply = None
    try:
        reply = http(sub["url"], headers, body)
        status = reply.status
        error = None
        if not 200 <= status < 300:
            # Keep the receiver's own explanation (scrubbed, short) so a 400 says why.
            detail = " ".join((reply.text or "").split())
            error = _scrub(f"HTTP {status}: {detail}" if detail else f"HTTP {status}", sub)
    except Exception as exc:  # network trouble: retry
        status = None
        error = _scrub(f"{type(exc).__name__}: {exc}", sub)
    del headers  # the token never goes further than the request
    if error is None:
        values = {
            "state": "sent",
            "sent_at": now,
            "http_status": status,
            "error": None,
            "link": _link(sub, reply),
        }
    elif status is None or status == 429 or status >= 500:
        if attempts >= MAX_TRIES:
            values = {"state": "gave_up", "http_status": status, "error": error}
        else:
            later = _retry_after(reply, now) or now + min(
                BACKOFF * (2 ** (attempts - 1)), MAX_BACKOFF
            )
            values = {"state": "retry", "http_status": status, "error": error, "next_at": later}
    else:  # 4xx other than 429: a wrong address or token does not fix itself
        values = {"state": "failed", "http_status": status, "error": error}
    _finish(rt, delivery, values)
    _audit_send(
        rt,
        sub,
        event,
        "ok" if values["state"] == "sent" else values["state"],
        {
            "attempt": attempts,
            "http_status": status,
            "activity_ids": ids,
            "error": values.get("error"),
        },
    )
    if values["state"] != "sent":
        log.warning("event %s to link %s: %s (%s)", event["id"], sub["id"], values["state"], error)
    return values["state"]


def _finish(rt, delivery, values: dict[str, Any]) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            update(agent_event_delivery)
            .where(agent_event_delivery.c.id == delivery["id"])
            .values(**values)
        )


def _claim(rt, delivery_id: int, now: datetime) -> dict[str, Any] | None:
    """Mark one due delivery ``sending`` (attempt counted) before any network call."""
    with rt.engine.begin() as conn:
        claimed = conn.execute(
            update(agent_event_delivery)
            .where(
                agent_event_delivery.c.id == delivery_id,
                agent_event_delivery.c.state.in_(OPEN),
            )
            .values(
                state="sending",
                attempts=agent_event_delivery.c.attempts + 1,
                attempted_at=now,
            )
        ).rowcount
        if not claimed:
            return None
        return dict(
            conn.execute(
                select(agent_event_delivery).where(agent_event_delivery.c.id == delivery_id)
            )
            .mappings()
            .one()
        )


def _recover(rt, now: datetime) -> None:
    """A delivery left ``sending`` by a crash may have gone out: never send it again."""
    with rt.engine.begin() as conn:
        conn.execute(
            update(agent_event_delivery)
            .where(
                agent_event_delivery.c.state == "sending",
                agent_event_delivery.c.attempted_at < now - STALE_SENDING,
            )
            .values(state="unknown", error="interrupted while sending; not sent again")
        )


def deliver(rt, *, now: datetime | None = None, http: Exchange | None = None) -> dict[str, int]:
    now = now or utc_now()
    http = http or _requests_exchange
    _recover(rt, now)
    counts: dict[str, int] = {}
    subs = _subscriptions(rt)
    with rt.engine.connect() as conn:
        due = (
            conn.execute(
                select(agent_event_delivery)
                .where(
                    agent_event_delivery.c.state.in_(OPEN),
                    agent_event_delivery.c.next_at <= now,
                )
                .order_by(agent_event_delivery.c.created_at, agent_event_delivery.c.id)
            )
            .mappings()
            .all()
        )
    for row in due:
        sub = subs.get(row["subscription_id"])
        state = _deliver_one(rt, sub, dict(row), now, http)
        counts[state] = counts.get(state, 0) + 1
    return counts


def _deliver_one(rt, sub, row, now: datetime, http: Exchange) -> str:
    if sub is None or not sub.get("enabled") or not sub.get("secret"):
        _finish(rt, row, {"state": "skipped", "error": "link off or removed"})
        return "skipped"
    with rt.engine.connect() as conn:
        event_row = (
            conn.execute(select(agent_event).where(agent_event.c.id == row["event_id"]))
            .mappings()
            .first()
        )
        sent_today = _sent_today(conn, rt, sub["id"], now)
    if event_row is None:
        _finish(rt, row, {"state": "skipped", "error": "event removed"})
        return "skipped"
    if now - _aware(event_row["occurred_at"]) > EXPIRE:
        _finish(rt, row, {"state": "expired", "error": "too old to send"})
        return "expired"
    if (until := quiet_until(rt, sub, now)) is not None:
        _finish(rt, row, {"next_at": until})
        return "quiet"
    if sent_today >= int(sub.get("daily_cap") or DAILY_CAP):
        _finish(
            rt,
            row,
            {"next_at": _day_start(rt, now) + timedelta(days=1), "error": "daily limit reached"},
        )
        return "capped"
    claimed = _claim(rt, row["id"], now)
    if claimed is None:
        return "taken"
    return _send(rt, sub, claimed, envelope(event_row), now, http)


def run(rt, *, now: datetime | None = None, http: Exchange | None = None) -> dict[str, Any]:
    """The scheduled job: watch, emit what is ready, send what is due, tidy up."""
    now = now or utc_now()
    migrate(rt, now=now)
    observe(rt, now=now)
    event_id = emit_ready(rt, now=now)
    published = emit_published(rt, now=now)
    missed = emit_missed(rt, now=now)
    sent = deliver(rt, now=now, http=http)
    _prune(rt, now)
    return {"event": event_id, "published": published, "missed": missed, "deliveries": sent}


def _prune(rt, now: datetime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            delete(agent_event_watch).where(agent_event_watch.c.first_seen_at < now - KEEP)
        )
        old = select(agent_event.c.id).where(agent_event.c.occurred_at < now - KEEP)
        conn.execute(
            delete(agent_event_delivery).where(
                agent_event_delivery.c.event_id.in_(old),
                agent_event_delivery.c.state.not_in((*OPEN, "sending")),
            )
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
            if _missed_unsent(rt, conn, now):
                return True
            return bool(
                conn.execute(
                    select(func.count())
                    .select_from(agent_event_delivery)
                    .where(
                        or_(
                            and_(
                                agent_event_delivery.c.state.in_(OPEN),
                                agent_event_delivery.c.next_at <= now,
                            ),
                            and_(
                                agent_event_delivery.c.state == "sending",
                                agent_event_delivery.c.attempted_at < now - STALE_SENDING,
                            ),
                        )
                    )
                ).scalar()
            )
    except Exception:
        log.exception("checking for agent events failed")
        return False


def after_derive(rt) -> None:
    """Called after every derive; never lets a problem here fail the sync."""
    try:
        observe(rt)
    except Exception:
        log.exception("watching new activities for agent events failed")


# --- test and status -----------------------------------------------------------------------


def send_test(
    rt, sub_id: str, *, now: datetime | None = None, http: Exchange | None = None
) -> dict:
    """Send one ``vibesynergy.test`` event now, outside quiet hours and the debounce, but within
    the daily cap. Returns the delivery as the page shows it."""
    now = now or utc_now()
    sub = _subscriptions(rt).get(sub_id)
    if sub is None:
        raise SubscriptionError("This link no longer exists.")
    if not sub.get("secret"):
        raise SubscriptionError("Save the secret or token first.")
    with rt.engine.connect() as conn:
        if _sent_today(conn, rt, sub_id, now) >= int(sub.get("daily_cap") or DAILY_CAP):
            raise SubscriptionError("The daily limit for this link is reached. Try again tomorrow.")
    event_id = _new_id()
    with rt.engine.begin() as conn:
        conn.execute(
            agent_event.insert().values(
                id=event_id, type=TEST, version=VERSION, occurred_at=now, data={}
            )
        )
        delivery_id = conn.execute(
            agent_event_delivery.insert().values(
                event_id=event_id,
                subscription_id=sub_id,
                state="pending",
                attempts=MAX_TRIES - 1,  # a test is tried once, never retried
                created_at=now,
                next_at=now,
            )
        ).inserted_primary_key[0]
    claimed = _claim(rt, delivery_id, now)
    with rt.engine.connect() as conn:
        event_row = (
            conn.execute(select(agent_event).where(agent_event.c.id == event_id)).mappings().one()
        )
    _send(rt, sub, claimed, envelope(event_row), now, http or _requests_exchange)
    return next(d for d in deliveries(rt, sub_id) if d["event_id"] == event_id)


def deliveries(rt, sub_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """The delivery log of one link, newest first (no request or response bodies)."""
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(agent_event_delivery, agent_event.c.type)
                .join(agent_event, agent_event.c.id == agent_event_delivery.c.event_id)
                .where(agent_event_delivery.c.subscription_id == sub_id)
                .order_by(
                    agent_event_delivery.c.created_at.desc(), agent_event_delivery.c.id.desc()
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )

    def iso(value):
        return _aware(value).isoformat() if value else None

    return [
        {
            "event_id": row["event_id"],
            "type": row["type"],
            "state": row["state"],
            "attempts": row["attempts"],
            "created_at": iso(row["created_at"]),
            "next_at": iso(row["next_at"]) if row["state"] in OPEN else None,
            "attempted_at": iso(row["attempted_at"]),
            "sent_at": iso(row["sent_at"]),
            "http_status": row["http_status"],
            "error": row["error"],
            "link": row["link"],
        }
        for row in rows
    ]


def overview(rt) -> dict[str, Any]:
    """Settings: the links (without secrets) with their recent deliveries, and the defaults."""
    migrate(rt)
    subs = subscriptions(rt)
    for sub in subs:
        sub["deliveries"] = deliveries(rt, sub["id"])
    return {
        "subscriptions": subs,
        "types": list(TYPES),
        "auth_modes": list(AUTH_MODES),
        "defaults": {
            "daily_cap": DAILY_CAP,
            "quiet_start": QUIET_START,
            "quiet_end": QUIET_END,
            "max": MAX_SUBSCRIPTIONS,
            "events": list(DEFAULT_EVENTS),
        },
    }
