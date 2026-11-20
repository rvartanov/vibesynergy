"""The contact window (#119): when an AI coach may reach the athlete.

One setting for every AI coach, the one inside VibeSynergy and any AI coach over MCP:
Coach → Settings, stored with the runner's settings as ``contact``
(``{"on": true, "start": "07:00", "end": "23:00"}``), in the install's time zone
(``settings.timezone``, Europe/Amsterdam by default). An end before the start spans
midnight (22:00-06:00 for night shifts).

**Outside the window entries are still written; only the delivery waits.** The entry is
stored and shown on the start page and the Coach page as always; what waits is every way
VibeSynergy reaches the athlete about it: the advice notice in the portal, the push and the
mail (``report.send``, also for an advice an AI coach wrote over MCP,
``daily_advice.announce``), the other report mails, the alert "Messages from your AI coach"
(kind ``daily_coach``) and the notice that a coach run failed. They are kept in the outbox
(``OUTBOX`` in the runner's store) and ``release`` sends them when the window opens (a
service job, checked on every scheduler tick). For a daily advice only the version that is
current then goes out, once per day: a version replaced overnight is never sent.

Not held: VibeSynergy's own alerts (a login, a quiet source, a failed sync, the rules'
health warnings, the budget mail) follow Settings → Alerts; webhooks to AI apps have their
own quiet hours (Settings → Sync between sources, ``agent_events``); the public insight line
is written into the activity's own description and is no message to the athlete; sending a
report again by hand is the athlete's own request. VibeSynergy sends no Garmin or Strava
messages; an AI coach over MCP is told the window in ``vibesynergy_coach_state`` and the
handbook and keeps to it on its own channels.

**Default 07:00-23:00.** The fixed advice run is 07:00 by default (``settings.
DEFAULT_SCHEDULE``), so the morning advice goes out the moment it is made, and the evening
review (21:45) and the week review (Sunday 19:44) fall inside too. A later start (08:00, say)
never blocks or moves the morning run: it runs at its own time, the advice is written and on
the page, and only its notice, push and mail wait until the window opens.

**Urgent safety exception.** One delivery may go out at once outside the window: a daily
advice for today (the athlete's local date) whose safety level is ``refuse`` (red) because
of a hard rule the athlete's own report set off: illness, a red-flag note, pain 7/10 or
more, or an injury that changes how he moves (``safety_floor.HARD_RULES``, recorded at the
write in ``meta.harness.rules_floor``). He logged it himself, so he is awake, and a session
planned early today should not wait. A red day from measurements alone (HRV and resting
heart rate, deep fatigue) waits for the window, and so does an advice for tomorrow, every
report that is no daily advice, and any message that only rewords the advice.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.timeutil import utc_now

log = logging.getLogger(__name__)

DEFAULT_WINDOW: dict[str, Any] = {"on": True, "start": "07:00", "end": "23:00"}
DEFAULT = DEFAULT_WINDOW  # what an unsaved window is
OUTBOX = "contact_outbox"
JOB = "coach_contact"  # the service job that releases the outbox
HELD = "waiting for the contact window"  # coach_run.mail_error while a report waits
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
URGENT_LEVEL = "refuse"
URGENT_TEXT = (
    "Outside the window, deliver nothing. One exception: a daily advice for the athlete's "
    "today whose safety level is refuse (red) because of a hard rule his own report set "
    "off (illness, a red-flag note, pain 7/10 or more, an injury that changes how he "
    "moves). A red day from measurements alone, an advice for tomorrow and every other "
    "message wait until the window opens."
)
WHAT_WAITS = (
    "the advice notice, push and mail",
    "report mails",
    "messages from your AI coach",
    "the notice that a coach run failed",
    "an AI coach's own messages to the athlete (chat, push, mail, Garmin or Strava)",
)


def normalise(saved: Any) -> dict[str, Any]:
    """The stored window with the defaults for anything missing or malformed."""
    saved = saved if isinstance(saved, dict) else {}
    out = dict(DEFAULT)
    if isinstance(saved.get("on"), bool):
        out["on"] = saved["on"]
    for key in ("start", "end"):
        if isinstance(saved.get(key), str) and _TIME.match(saved[key]):
            out[key] = saved[key]
    if out["start"] == out["end"]:
        out["start"], out["end"] = DEFAULT_WINDOW["start"], DEFAULT_WINDOW["end"]
    return out


def check(value: Any, current: dict[str, Any]) -> dict[str, Any]:
    """A change from the settings page; a ``ValueError`` in plain English otherwise."""
    if not isinstance(value, dict) or set(value) - {"on", "start", "end"}:
        raise ValueError("Unknown setting.")
    out = {**current, **value}
    if not isinstance(out["on"], bool):
        raise ValueError("Choose on or off.")
    for key in ("start", "end"):
        if not isinstance(out[key], str) or not _TIME.match(out[key]):
            raise ValueError("Give the time as hours and minutes, e.g. 21:45.")
    if out["start"] == out["end"]:
        raise ValueError("The contact window needs a start and an end that differ.")
    return {"on": out["on"], "start": out["start"], "end": out["end"]}


def spec_for(rt) -> dict[str, Any]:
    from vibesynergy.coach_runner import settings as runner_settings

    try:
        return runner_settings.load(rt).contact or dict(DEFAULT)
    except Exception:
        log.debug("reading the contact window failed; using the default", exc_info=True)
        return dict(DEFAULT)


def zone(rt) -> ZoneInfo:
    return ZoneInfo(getattr(rt.settings, "timezone", None) or "Europe/Amsterdam")


def is_open(spec: dict[str, Any], local: datetime) -> bool:
    """Inside the window at this local moment (always, when the window is off)."""
    if not spec.get("on", True):
        return True
    start, end = time.fromisoformat(spec["start"]), time.fromisoformat(spec["end"])
    now = local.time().replace(second=0, microsecond=0)
    if start < end:
        return start <= now < end
    return now >= start or now < end  # spans midnight


def opens_at(spec: dict[str, Any], local: datetime) -> datetime | None:
    """When the window opens next, or ``None`` while it is open."""
    if is_open(spec, local):
        return None
    start = time.fromisoformat(spec["start"])
    moment = datetime.combine(local.date(), start, tzinfo=local.tzinfo)
    return moment if moment > local else moment + timedelta(days=1)


def closed_at(spec: dict[str, Any], local: datetime) -> datetime | None:
    """When the window last closed (its most recent end before ``local``); ``None`` when
    the window is off."""
    if not spec.get("on", True):
        return None
    end = datetime.combine(local.date(), time.fromisoformat(spec["end"]), tzinfo=local.tzinfo)
    return end if end <= local else end - timedelta(days=1)


def coach_since(rt, since: datetime | None, now: datetime | None = None) -> datetime | None:
    """From when the alert "Messages from your AI coach" reads new entries (``alerts``):
    nothing while the window is closed (``None``); once open, back to when it closed, so
    an entry written outside the window is sent now. The alert's own 24-hour dedupe keeps
    one that was already sent from going out again."""
    if since is None:
        return None
    spec = spec_for(rt)
    local = (now or utc_now()).astimezone(zone(rt))
    if not is_open(spec, local):
        return None
    closed = closed_at(spec, local)
    if closed is None:
        return since
    closed = closed.astimezone(UTC)
    if since.tzinfo is None:
        closed = closed.replace(tzinfo=None)
    return min(since, closed)


def allowed(rt, *, urgent: bool = False, now: datetime | None = None) -> bool:
    """May an AI coach's delivery go out now?"""
    if urgent:
        return True
    return is_open(spec_for(rt), (now or utc_now()).astimezone(zone(rt)))


def urgent(rt, entry: dict[str, Any], advice_day: date | None, now: datetime | None = None) -> bool:
    """The urgent safety exception (module docstring): today's advice, red, from a hard
    rule the athlete's own report set off."""
    from vibesynergy.query import daily_advice, safety_floor

    if advice_day is None:
        return False
    today = (now or utc_now()).astimezone(zone(rt)).date()
    if advice_day != today:
        return False
    meta = entry.get("meta") or {}
    safety = daily_advice.ai_safety(meta)
    if not safety or safety.get("level") != URGENT_LEVEL:
        return False
    floor = (meta.get("harness") or {}).get("rules_floor") or {}
    return bool(set(floor.get("rules") or []) & safety_floor.HARD_RULES)


def state(rt, now: datetime | None = None) -> dict[str, Any]:
    """The window as the handbook, ``vibesynergy_coach_state`` and the portal show it."""
    spec = spec_for(rt)
    tz = zone(rt)
    local = (now or utc_now()).astimezone(tz)
    opening = opens_at(spec, local)
    return {
        "on": spec["on"],
        "start": spec["start"],
        "end": spec["end"],
        "timezone": tz.key,
        "open_now": opening is None,
        "opens_at": opening.isoformat() if opening else None,
        "waiting": len(outbox(rt)),
        "what_waits": list(WHAT_WAITS),
        "rule": (
            f"Reach the athlete only between {spec['start']} and {spec['end']} "
            f"({tz.key}). Outside it, write your entries as usual; VibeSynergy holds its "
            "notice, push and mail until the window opens, and you hold your own messages."
            if spec["on"]
            else "No contact window: an AI coach may reach the athlete at any time."
        ),
        "urgent_exception": URGENT_TEXT,
    }


# --- the outbox ----------------------------------------------------------------------------


def _store(rt):
    from vibesynergy.coach_runner import settings as runner_settings

    return runner_settings.store(rt)


def outbox(rt) -> list[dict[str, Any]]:
    try:
        found = _store(rt).get(OUTBOX) or {}
    except Exception:
        return []
    items = found.get("items") if isinstance(found, dict) else None
    return list(items) if isinstance(items, list) else []


def _add(rt, item: dict[str, Any]) -> None:
    store = _store(rt)
    with store.lock(OUTBOX):
        items = outbox(rt)
        key = item.get("key")
        items = [i for i in items if not key or i.get("key") != key]
        items.append({**item, "held_at": utc_now().isoformat()})
        store.set(OUTBOX, {"items": items})


def hold_report(rt, *, job: str, day: date, entry_id: int, advice_day: date | None = None) -> None:
    """Keep a report (a coach entry) until the window opens."""
    _add(
        rt,
        {
            "type": "report",
            "key": f"report:{entry_id}",
            "job": job,
            "day": day.isoformat(),
            "entry_id": int(entry_id),
            "advice_day": advice_day.isoformat() if advice_day else None,
        },
    )


def hold_message(
    rt, *, key: str, subject: str, body: str, only: list[str], url: str | None = None
) -> None:
    """Keep a plain message (a failed run's notice) until the window opens."""
    _add(
        rt,
        {
            "type": "message",
            "key": key,
            "subject": subject,
            "body": body,
            "only": list(only),
            "url": url,
        },
    )


def is_held(rt, entry_id: int) -> bool:
    return any(i.get("entry_id") == entry_id for i in outbox(rt))


def due(rt, now: datetime | None = None) -> bool:
    """Something waits and the window is open (asked on every scheduler tick)."""
    try:
        return bool(outbox(rt)) and allowed(rt, now=now)
    except Exception:
        log.exception("checking the contact window outbox failed")
        return False


def _job_of(rt, entry: dict[str, Any], fallback: str) -> str:
    """The task that wrote an advice version: its run's job, else the harness task."""
    from sqlalchemy import select

    from vibesynergy.query import daily_advice
    from vibesynergy.store.schema import coach_run

    with rt.engine.connect() as conn:
        job = conn.scalar(
            select(coach_run.c.job)
            .where(coach_run.c.entry_id == entry["id"])
            .order_by(coach_run.c.id.desc())
            .limit(1)
        )
    task = ((entry.get("meta") or {}).get("harness") or {}).get("task")
    for found in (job, task):
        if found in daily_advice.ADVICE_JOBS:
            return str(found)
    if entry.get("entry_type") == "evening":
        return "evening"
    return fallback if fallback in daily_advice.ADVICE_JOBS else "morning"


def _mark_mailed(rt, entry_id: int) -> None:
    from sqlalchemy import update

    from vibesynergy.store.schema import coach_run

    with rt.engine.begin() as conn:
        conn.execute(
            update(coach_run)
            .where(coach_run.c.entry_id == entry_id, coach_run.c.mailed_at.is_(None))
            .values(mailed_at=utc_now(), mail_error=None)
        )


def _clear_held(rt, entry_ids: list[int]) -> None:
    """A version replaced while it waited is never sent: its run no longer says it waits."""
    from sqlalchemy import update

    from vibesynergy.store.schema import coach_run

    if not entry_ids:
        return
    with rt.engine.begin() as conn:
        conn.execute(
            update(coach_run)
            .where(coach_run.c.entry_id.in_(entry_ids), coach_run.c.mail_error == HELD)
            .values(mail_error=None)
        )


def release(rt, now: datetime | None = None) -> dict[str, Any]:
    """Send what waited, once the window is open. Returns what went out."""
    from vibesynergy import alerts
    from vibesynergy.coach_runner import report
    from vibesynergy.query import daily_advice

    if not allowed(rt, now=now):
        return {"sent": [], "skipped": [], "waiting": len(outbox(rt))}
    store = _store(rt)
    with store.lock(OUTBOX):
        items = outbox(rt)
        store.set(OUTBOX, {"items": []})
    sent: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, Any]] = []
    days: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("type") == "report" and item.get("advice_day"):
            days.setdefault(item["advice_day"], []).append(item)
    for day, held in sorted(days.items()):
        # Only the version that is current now, once for the day (a version replaced
        # overnight is never sent; the newest is, whichever version it is).
        try:
            current = None
            for item in held:
                found = daily_advice.version_state(rt, int(item["entry_id"]))
                if found and found.get("current_id"):
                    current = int(found["current_id"])
                    break
            entry = report.stored(rt, current) if current else None
            if entry is None:
                skipped.append(f"advice:{day}")
                continue
            job = _job_of(rt, entry, held[-1].get("job") or "morning")
            used = report.send(
                rt, job, entry["for_date"], entry, advice_day=date.fromisoformat(day)
            )
            if used:
                _mark_mailed(rt, int(entry["id"]))
                sent.append(f"advice:{day}")
        except Exception:
            log.exception("contact window: sending the held advice for %s failed", day)
            failed.extend(held)
    for item in items:
        if item.get("type") == "report" and item.get("advice_day"):
            continue
        try:
            if item.get("type") == "report":
                entry = report.stored(rt, item["entry_id"])
                if entry is None:
                    skipped.append(item["key"])
                    continue
                used = report.send(rt, item["job"], date.fromisoformat(item["day"]), entry)
                if used:
                    _mark_mailed(rt, int(entry["id"]))
            else:
                extra = {"url": item["url"]} if item.get("url") else {}
                used = alerts.deliver(
                    rt, item["subject"], item["body"], only=item.get("only"), **extra
                )
            if used:
                sent.append(item["key"])
        except Exception:
            log.exception("contact window: sending %s failed", item.get("key"))
            failed.append(item)
    _clear_held(rt, [int(i["entry_id"]) for i in items if i.get("type") == "report"])
    if failed:
        with store.lock(OUTBOX):
            store.set(OUTBOX, {"items": [*outbox(rt), *failed]})
    return {"sent": sent, "skipped": skipped, "waiting": len(outbox(rt))}
