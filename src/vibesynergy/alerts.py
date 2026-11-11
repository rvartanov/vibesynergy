"""Alerts: tell the user in the portal and by email when something needs attention, each
kind on the channels chosen for it (Settings -> Alerts).

Checked every ALERT_MINUTES by the service. A message goes out when a measurement or the
system needs action. A complaint the athlete logged is not mailed, and neither is a red day
that only follows from that complaint. The same warning is not repeated the same day. A
login or a quiet source does get one note when it is back. One-off events (a failed job, a
finished history run) are sent once. The kinds are in KINDS.

Configuration under ``[alerts]``: ``enabled`` (default true) and ``email_to``, sent through
the generic mailer (``[mail]``, see ``vibesynergy.mailer``). Nothing secret is in the config
file. Which kinds go to the portal and which by email is a portal preference
(``alert_routes``).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy import i18n, mailer
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.portal import prefs
from vibesynergy.query import dashboard, workouts
from vibesynergy.store.schema import history_fill

log = logging.getLogger(__name__)

ALERT_MINUTES = 15
SECRETS = "alerts"
STATE = "state"
CHECKED = "_checked_at"  # in the state: when the previous check ran
RECENT = "_recent"  # in the state: the latest one-off events, for the portal
SENT = "_sent"  # when a one-off, or an advice alert, was last mailed (ISO, or ISO|level)
DELIVERIES = "_deliveries"  # external outcomes, by channel and alert episode
EPISODE = "_episode"  # monotonically increasing identity for each new alert episode
DEDUPE_HOURS = 24
LABEL = {"stop": "STOP", "warn": "Attention", "info": "Note"}
# Health warnings: ending them adds nothing new, and the same line waits a day.
ADVICE_KINDS = frozenset({"injury", "health"})
# A red day is mailed only when one of these fired. The others are notes the athlete typed.
MEASURED_RED = frozenset({"hrv_rhr", "deep_fatigue"})
_RANK = {"info": 0, "warn": 1, "stop": 2}
_VOLATILE = re.compile(r"\d+(?:[.,]\d+)?")
_NOTE_EXCERPT = 300


CHANNELS = ("portal", "email", "push")
ALL = list(CHANNELS)
# kind -> (what it is, the channels it goes to until chosen otherwise)
KINDS: dict[str, tuple[str, list[str]]] = {
    "injury": ("Injury warning flags (pain that changes how you move, …)", ALL),
    "health": ("Recovery and training warnings (a red day, low HRV, heavy load)", ALL),
    "login": ("A login that stopped working", ALL),
    "silent": ("A source that stopped syncing", ALL),
    "job": ("A sync job that failed", ALL),
    "history": ("A history run finished or got stuck", ["portal", "email"]),
    "import": ("An account export import finished or failed", ["portal", "email"]),
    "daily_coach": ("Messages from your coach", ["portal", "push"]),
    "token": ("A coach token is about to expire", ["portal", "email"]),
}
RECENT_KEPT = 20  # one-off events kept for the portal's list


@dataclass(frozen=True)
class Alert:
    key: str  # stable: the same problem keeps its key while it lasts
    level: str  # stop | warn | info
    text: str
    link: str = ""
    event: bool = False  # a one-off (a failed job): sent once, never "resolved"
    kind: str = "health"
    dedupe: str = ""  # events with the same value are sent at most once per DEDUPE_HOURS
    detail: str = ""
    external: bool = True  # email; a logged complaint stays on the portal only
    rich: dict | None = None  # a coach note's parts for the HTML mail


def _plain_error(error: str) -> str:
    """The error without exception class names and tracebacks: what is wrong, in one line."""
    error = error.strip().splitlines()[-1] if error.strip() else ""
    error = re.sub(r"\b(?:[A-Za-z_][\w.]*(?:Error|Exception|Limited)):\s*", "", error)
    return re.sub(r"\s+", " ", error).strip()


def _job_alert(job: dict[str, Any], tr: i18n.Translator) -> Alert:
    payload = job["payload"]
    raw_error = str(job["last_error"] or "")
    error = _plain_error(raw_error)
    dataset = payload.get("dataset") or ""
    calendar = payload.get("source") == "caldav"
    login = bool(
        re.search(
            r"login|sign.?in|token|unauthori[sz]ed|401|403|credentials|password|expired|reauth",
            raw_error,
            re.I,
        )
    )
    network = bool(
        re.search(
            r"network|connect|timeout|timed out|unreachable|temporar|50[234]", raw_error, re.I
        )
    )
    if calendar and login:
        text = tr("Your calendar couldn't be read. Sign in again in Settings → Calendar.")
    elif calendar and network:
        text = tr("Your calendar couldn't be read. Try again later.")
    elif calendar:
        text = tr("Your calendar couldn't be read. Check Settings → Calendar or try again later.")
    elif login:
        text = tr("Your data couldn't be updated. Sign in again in Settings → Data sources.")
    elif network:
        text = tr("Your data couldn't be updated. Try again later.")
    else:
        text = tr("Your data couldn't be updated. Check Data → Sources or try again later.")
    normalized = re.sub(r"\d+", "#", error.lower())
    signature = f"{payload.get('source')}|{dataset}|{normalized}"
    source = payload.get("source") or "unknown"
    link = "#/settings/calendar" if calendar else "#/settings/data/sources"
    sig_hash = hashlib.sha256(signature.encode()).hexdigest()[:12]
    return Alert(
        f"job-source:{source}:{sig_hash}",
        "warn",
        text,
        link,
        event=True,
        kind="job",
        dedupe=signature,
        detail=error[:500],
    )


def routes(settings: Any) -> dict[str, list[str]]:
    """Channels per kind: the defaults, overridden by the choices in the preferences."""
    chosen = prefs.load(settings.data_dir).get("alert_routes") or {}
    # A channel that no longer exists (WhatsApp, until 31-10-2026) is skipped.
    return {
        kind: [c for c in chosen.get(kind, default) if c in CHANNELS]
        for kind, (_, default) in KINDS.items()
    }


def _kind(item: dict[str, str]) -> str:
    if item["area"] == "injury":
        return "injury"
    if item["area"] == "system":
        return "login" if "login" in item["message"] else "silent"
    return "health"


def _rank(level: str) -> int:
    return _RANK.get(level, 1)


def _alert_key(area: str, message: str) -> str:
    """One open problem. A changing count (hours, days, bpm) is not a new problem."""
    head = message.split(":", 1)[0]
    return f"{area}:{_VOLATILE.sub('#', head)}"


def _identity(key: str) -> str:
    return _VOLATILE.sub("#", key)


def _sent_moment(value: str) -> datetime:
    return datetime.fromisoformat(value.split("|", 1)[0])


def _sent_level(value: str) -> str:
    return value.split("|", 1)[1] if "|" in value else "warn"


def _pushes(alert: Alert) -> bool:
    """Email. A complaint the athlete logged is not news; the portal still lists it."""
    return alert.kind != "injury" and alert.external


def _advice_repeat(alert: Alert, sent_at: dict[str, str]) -> bool:
    """This recovery line was already mailed, and it did not get stricter."""
    if alert.event or alert.kind not in ADVICE_KINDS:
        return False
    raw = sent_at.get(alert.key)
    if not raw:
        return False
    return _rank(alert.level) <= _rank(_sent_level(raw))


def _held(key: str, hold: frozenset[str]) -> bool:
    """A failed reading is not an all-clear. Keep the previous open alerts."""
    if "verdict" in hold and key.startswith("verdict:"):
        return True
    return "attention" in hold and not key.startswith(("verdict:", "history:", "token:"))


def _fill_id(key: str) -> str | None:
    parts = key.split(":")
    if len(parts) >= 3 and parts[0] == "history":
        return parts[1]
    return None


def _measured_red(verdict: dict) -> bool:
    """The red day comes from a reading the athlete did not enter (HRV, resting HR, fatigue)."""
    return bool(set(verdict.get("rules") or []) & MEASURED_RED)


def _red_notice(verdict: dict, *, measured: bool) -> str:
    """The action for a red day. A measurement can add one reason; a logged complaint does not."""
    advice = " ".join(str(verdict.get("advice") or verdict.get("headline") or "").split())
    if not advice or not measured:
        return advice
    caveat = ""
    for item in verdict.get("signals") or []:
        if item.get("rule") not in MEASURED_RED:
            continue
        words = " ".join(str(item.get("text") or "").split()).strip(" .!?")
        if words:
            caveat = words[:1].upper() + words[1:]
            break
    if not caveat:
        return advice
    if advice[-1] not in ".!?":
        advice += "."
    return f"{advice} {caveat}."


def _unique(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = " ".join(line.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _note_excerpt(note: str) -> str:
    """One line of the coach note for the alert title. The mail still carries the full note."""
    compact = " ".join(note.split())
    if len(compact) <= _NOTE_EXCERPT:
        return compact
    return compact[: _NOTE_EXCERPT - 1].rstrip() + "…"


def _new_mail_lines(records: list[dict]) -> list[str]:
    """Warning text collapses when it repeats. Each coach note stays, even with the same words."""
    lines: list[str] = []
    seen: set[str] = set()
    for record in records:
        line = f"{LABEL[record['level']]}: {record['text']}"
        detail = str(record.get("detail") or "").strip()
        coach_note = record.get("kind") == "daily_coach"
        # A job's detail is the raw error. A coach note's detail is the message itself:
        # shown once, without the label and excerpt in front.
        if coach_note and detail:
            line = detail
        key = str(record.get("id") or line) if coach_note else " ".join(line.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return lines


def _headline(text: str, limit: int = 70) -> str:
    """The first clause of a note, cut at a word, for a mail subject."""
    compact = " ".join(text.split())
    for stop in (" (", ". ", "; "):
        if stop in compact:
            compact = compact.split(stop, 1)[0]
    if len(compact) <= limit:
        return compact
    cut = compact[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,:;") + "…"


def _subject(records: list[dict]) -> str:
    first = records[0]
    if all(r.get("kind") == "daily_coach" for r in records):
        return f"VibeSynergy coach: {_headline(str(first.get('detail') or first['text']))}"
    return f"VibeSynergy: {_headline(first['text'])}"


def _mail_footer_link(records: list[dict]) -> str:
    if records and all(record.get("kind") == "daily_coach" for record in records):
        return "#/coach"
    return "#/dashboard"


class Collected(list):
    """The alerts that hold now. ``hold`` names readings that failed, so those stay open."""

    hold: frozenset[str]

    def __init__(
        self, items: list[Alert] | None = None, hold: frozenset[str] = frozenset()
    ) -> None:
        super().__init__(items or [])
        self.hold = hold


def collect(rt: Any, today: date, since: datetime | None = None) -> list[Alert]:
    """The alerts that hold right now, plus jobs that failed after ``since`` (the previous
    check; none on the first check, so old failures don't alert)."""
    settings = rt.settings
    data = dashboard.dashboard(
        rt.engine,
        today,
        timezone=settings.timezone,
        athlete=settings.athlete,
        sources=rt.registry.enabled_ids(),
        gear=workouts.gear(rt.engine, rt.raw, today)["gear"],
        tr=i18n.for_settings(settings),
        rt=rt,
    )
    if data.get("onboarding"):
        return Collected()
    attention_raw = data.get("attention")
    verdict = data.get("verdict") or {}
    hold: set[str] = set()
    if isinstance(attention_raw, dict):
        hold.add("attention")
        attention: list = []
    else:
        attention = attention_raw or []
    if isinstance(verdict, dict) and verdict.get("error") == "unavailable":
        hold.add("verdict")
    chosen: dict[str, Alert] = {}
    for item in attention:
        if item["level"] not in ("stop", "warn") and item["area"] != "system":
            continue
        level = "stop" if item["level"] == "stop" else "warn"
        key = _alert_key(item["area"], item["message"])
        kind = _kind(item)
        alert = Alert(
            key,
            level,
            item["message"],
            item.get("link", ""),
            kind=kind,
            external=kind != "injury",
        )
        previous = chosen.get(key)
        if previous is None or _rank(level) > _rank(previous.level):
            chosen[key] = alert
    out: list[Alert] = list(chosen.values())
    if "verdict" not in hold and isinstance(verdict, dict) and verdict.get("level") == "red":
        measured = _measured_red(verdict)
        text = _red_notice(verdict, measured=measured)
        if text:
            # His own complaint already says why. Mail only when a reading adds a reason.
            out.append(Alert("verdict:red", "warn", text, "#/dashboard", external=measured))
    for job in rt.jobs.recent(status="dead", limit=20) if since else []:
        error = str(job["last_error"] or "")
        if not job["finished_at"] or job["finished_at"] <= since or "RateLimited" in error:
            continue  # older, or a rate limit (those wait for the reset by themselves)
        source = job["payload"].get("source")
        if source not in rt.registry.enabled_ids() or not rt.registry.get(source).configured():
            continue
        out.append(_job_alert(job, i18n.for_settings(settings)))
    out += _history(rt, since)
    out += _daily(rt, since, today)
    out += _expiring_tokens(rt)
    return Collected(out, frozenset(hold))


def _expiring_tokens(rt: Any) -> list[Alert]:
    """Portal and email warning during the 7 days before a coach token expires."""
    from sqlalchemy import select

    from vibesynergy.core.user_datetime import format_user_datetime
    from vibesynergy.store.schema import mcp_pat

    now = utc_now()
    horizon = now + timedelta(days=7)
    tr = i18n.for_settings(rt.settings)
    out: list[Alert] = []
    with rt.engine.connect() as conn:
        rows = conn.execute(select(mcp_pat)).all()
    for row in rows:
        if row.revoked_at is not None or row.expires_at is None:
            continue
        if not (now < row.expires_at <= horizon):
            continue
        name = row.display_name or row.name
        when = format_user_datetime(row.expires_at)
        text = tr(
            "{name} expires on {when}. Make a new token in Settings → Coach apps.",
            name=name,
            when=when,
        )
        out.append(
            Alert(
                f"token:{row.id}",
                "info",
                text,
                "#/settings/sharing",
                kind="token",
            )
        )
    return out


def _history(rt: Any, since: datetime | None) -> list[Alert]:
    """A stuck run is open until it moves on; a finished or failed one is an event."""
    history_link = "#/settings/data/history"
    with rt.engine.connect() as conn:
        fills = [dict(r._mapping) for r in conn.execute(select(history_fill))]
    out = []
    for f in fills:
        kind = "import" if f["method"] == "archive" else "history"
        what = f"{f['source'].title()} {'export import' if kind == 'import' else 'history run'}"
        if f["status"] == "stuck":
            text = f"{what} is stuck: {f['message'] or f['last_error'] or 'no progress'}"
            out.append(Alert(f"history:{f['id']}:stuck", "warn", text, history_link, kind=kind))
        finished = f["finished_at"]
        if since and finished and _aware(finished) > since and f["status"] in ("done", "failed"):
            state = "finished" if f["status"] == "done" else f"failed: {f['last_error'] or ''}"
            text = f"{what} {state}".strip()
            level = "info" if f["status"] == "done" else "warn"
            key = f"history:{f['id']}:{f['status']}"
            out.append(Alert(key, level, text[:200], history_link, event=True, kind=kind))
    return out


def _daily(rt: Any, since: datetime | None, delivery_day: date | None = None) -> list[Alert]:
    """A new morning note from a coach. This reads the coach feed, not internal reviews.

    ``delivery_day`` is unused. The alert is the note itself, not a generated review.
    """
    del delivery_day
    if not since:
        return []
    from vibesynergy.store.schema import coach_decision_log

    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(
                coach_decision_log.c.id,
                coach_decision_log.c.at,
                coach_decision_log.c.text,
                coach_decision_log.c.meta,
            )
            .where(
                coach_decision_log.c.entry_type == "morning",
                coach_decision_log.c.at > since,
            )
            .order_by(coach_decision_log.c.id)
        ).all()
        from vibesynergy.store.schema import coach_run

        # A note the API coach runner wrote has its own report mail: not twice.
        by_runner = {
            entry
            for (entry,) in conn.execute(
                select(coach_run.c.entry_id).where(coach_run.c.entry_id.is_not(None))
            )
        }
    rows = [r for r in rows if r[0] not in by_runner]
    tr = i18n.for_settings(rt.settings)
    heading = tr("Messages from your coach")
    notes = []
    for ident, _at, note, meta in rows:
        raw = str(note or "").strip()
        if not raw:
            continue
        lines = _session_lines((meta or {}).get("sessions"), tr)
        if lines:
            raw = raw + "\n\n" + "\n".join(lines)
        notes.append(
            Alert(
                f"coach:{ident}",
                "info",
                f"{heading}: {_note_excerpt(raw)}",
                "#/coach",
                event=True,
                kind="daily_coach",
                dedupe=str(ident),
                detail=raw,
                rich={
                    "text": str(note or "").strip(),
                    "sessions": (meta or {}).get("sessions") or [],
                    "details": (meta or {}).get("details") or "",
                },
            )
        )
    return notes


# The coach's verdict per session in the mail: the same words as the Coach page,
# with a plain-text mark instead of the coloured chip.
SESSION_MARKS = {
    "keep": ("✓", "Keep"),
    "adjust": ("~", "Adjust"),
    "skip": ("-", "Skip"),
    "done": ("✓", "Done"),
    "missed": ("!", "Missed"),
    "proposal": ("?", "Proposal"),
}


def _session_lines(sessions: Any, tr: Any) -> list[str]:
    out = []
    for item in sessions if isinstance(sessions, list) else []:
        if not isinstance(item, dict) or item.get("decision") not in SESSION_MARKS:
            continue
        mark, label = SESSION_MARKS[item["decision"]]
        note = f" ({item['note']})" if item.get("note") else ""
        out.append(f"{mark} {tr(label)}: {item.get('title', '')}{note}")
    return out


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def check(rt: Any, *, send: Any = None) -> dict[str, list[str]]:
    """Compare with what was sent before. Mail a new action, a stricter warning, or an
    operational recovery. An injury or a red day ending is not a mail. The portal's share
    (open alerts and recent events) stays in the state for the page to read. Returns the keys."""
    store = SecretStore(rt.settings.secrets_dir, SECRETS)
    with store.lock(STATE):
        return _check_locked(rt, store, send)


def _check_locked(rt: Any, store: SecretStore, send: Any) -> dict[str, list[str]]:
    """Keep concurrent checks from claiming the same external delivery."""
    state = store.get(STATE) or {}
    deliveries = state.pop(DELIVERIES, {})
    for record in deliveries.values():
        if record["status"] == "attempting":
            record["status"] = "ambiguous"
            log.warning("Alert delivery outcome unknown: %s", record["id"])
    # Persist the classification before collecting or delivering anything else.
    if deliveries:
        store.set(STATE, {**state, DELIVERIES: deliveries})
    checked = state.pop(CHECKED, None)
    recent = state.pop(RECENT, [])
    sent_at = state.pop(SENT, {})
    episode = state.pop(EPISODE, 0)
    since = datetime.fromisoformat(checked) if checked else None
    state = {k: v for k, v in state.items() if not k.startswith("job:")}  # events aren't open
    now = collect(rt, today_in(rt.settings.timezone), since)
    hold = getattr(now, "hold", frozenset())
    moment = utc_now()
    stamp = moment.isoformat()
    fresh = moment - timedelta(hours=DEDUPE_HOURS)
    sent_at = {k: v for k, v in sent_at.items() if _sent_moment(v) > fresh}
    events = []
    for a in (a for a in now if a.event):
        if a.dedupe and a.dedupe in sent_at:
            continue  # the same problem was already sent in the last 24 hours
        if a.dedupe:
            sent_at[a.dedupe] = stamp
        events.append(a)
    current = {a.key: a for a in now if not a.event}
    by_identity: dict[str, str] = {}
    for key in state:
        by_identity.setdefault(_identity(key), key)
    matched = {key: by_identity[_identity(key)] for key in current if _identity(key) in by_identity}
    new = [a for key, a in current.items() if key not in matched] + events
    chosen = routes(rt.settings)
    ready = [c for c, ok in channels(rt.settings, rt.engine).items() if ok]
    open_state: dict[str, dict] = {}
    escalations: list[Alert] = []
    for key, a in current.items():
        previous = state.get(matched[key]) if key in matched else None
        if previous is None:
            episode += 1
            base = {"since": stamp, "text": a.text, "episode": episode}
        else:
            base = dict(previous)
            if _rank(a.level) > _rank(str(previous.get("level") or "warn")):
                escalations.append(a)
        open_state[key] = base | {
            "kind": a.kind,
            "level": a.level,
            "link": a.link,
            "text": a.text,
        }
    resolved: list[str] = []
    matched_state = set(matched.values())
    for key, previous in state.items():
        if key in matched_state:
            continue
        if _held(key, hold):
            open_state[key] = previous
            continue
        resolved.append(key)
    outgoing = [a for a in new if _pushes(a) and not _advice_repeat(a, sent_at)]
    outgoing.extend(a for a in escalations if _pushes(a) and not _advice_repeat(a, sent_at))
    escalation_keys = {a.key for a in escalations}
    finished_runs: set[str] = set()
    for alert in outgoing:
        if not alert.event:
            continue
        fill = _fill_id(alert.key)
        if fill:
            finished_runs.add(fill)
    for a in outgoing:
        if a.event:
            episode += 1
            event_id = episode
        else:
            event_id = open_state[a.key].get("episode", open_state[a.key]["since"])
        for channel in ready:
            if channel not in chosen.get(a.kind, ALL):
                continue
            if a.key in escalation_keys and not a.event:
                ident = f"{channel}:escalate:{a.key}:{event_id}:{a.level}"
            else:
                ident = f"{channel}:new:{a.key}:{event_id}"
            record = {
                "id": ident,
                "status": "pending",
                "at": stamp,
                "channel": channel,
                "phase": "new",
                "text": a.text,
                "level": a.level,
                "detail": a.detail,
                "link": a.link,
                "kind": a.kind,
            }
            if a.rich:
                record["rich"] = a.rich
            if a.kind in ADVICE_KINDS and not a.event:
                record["dedupe_key"] = a.key
            deliveries[ident] = record
    for key in resolved:
        previous = state[key]
        kind = previous.get("kind", "health")
        fill = _fill_id(key)
        if kind in ADVICE_KINDS or (fill and fill in finished_runs):
            continue  # ending advice, or a run that already has its own finished note
        for channel in ready:
            if channel in chosen.get(kind, ALL):
                ident = f"{channel}:resolved:{key}:{previous.get('episode', previous['since'])}"
                deliveries[ident] = {
                    "id": ident,
                    "status": "pending",
                    "at": stamp,
                    "channel": channel,
                    "phase": "resolved",
                    "text": previous["text"],
                }
    recent = [
        {
            "key": a.key,
            "at": stamp,
            "text": a.text,
            "level": a.level,
            "link": a.link,
            "kind": a.kind,
            "detail": a.detail,
        }
        for a in events
    ] + recent
    # Commit the portal state and every channel's pending work before any external call.
    # SecretStore.set atomically replaces and fsyncs the journal; there is no open SQL
    # transaction for the watchdog's hard exit to discard.
    saved = {
        CHECKED: stamp,
        RECENT: recent[:RECENT_KEPT],
        SENT: sent_at,
        EPISODE: episode,
        DELIVERIES: deliveries,
        **open_state,
    }
    store.set(STATE, saved)
    send = send or (
        lambda subject, body, channel, html=None: deliver(
            rt, subject, body, only=[channel], html=html
        )
    )
    tr = i18n.for_settings(rt.settings)
    for channel in ready:
        for phase in ("new", "resolved"):
            pending = [
                record
                for record in deliveries.values()
                if record["status"] == "pending"
                and record["channel"] == channel
                and record["phase"] == phase
            ]
            if not pending:
                continue
            for record in pending:
                record["status"] = "attempting"
            store.set(STATE, saved)
            if phase == "new":
                lines = _new_mail_lines(pending)
                subject = _subject(pending)
                footer_link = _mail_footer_link(pending)
            else:
                lines = _unique([tr("Resolved: {message}", message=r["text"]) for r in pending])
                subject = tr("VibeSynergy: resolved")
                footer_link = "#/dashboard"
            if not lines:
                for record in pending:
                    record["status"] = "delivered"
                store.set(STATE, saved)
                continue
            text = "\n\n".join(lines) + _footer(rt, footer_link)
            if channel == "email" and phase == "new":
                outcome = _send_with_html(send, subject, text, channel, rt, pending, footer_link)
            else:
                outcome = send(subject, text, channel)
            delivered = outcome is None or channel in outcome
            for record in pending:
                record["status"] = "delivered" if delivered else "failed"
                if delivered and record.get("dedupe_key"):
                    sent_at[record["dedupe_key"]] = f"{stamp}|{record.get('level', 'warn')}"
            store.set(STATE, saved)
    # Keep all uncertain outcomes, plus a bounded history of completed channels.
    complete = [key for key, value in deliveries.items() if value["status"] == "delivered"]
    for key in complete[:-100]:
        del deliveries[key]
    store.set(STATE, saved)
    return {
        "new": [a.key for a in new],
        "resolved": resolved,
        "open": list(open_state),
        "ambiguous": [key for key, value in deliveries.items() if value["status"] == "ambiguous"],
    }


INTERNAL = (CHECKED, RECENT, SENT, DELIVERIES, EPISODE)


def _open(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, **value} for key, value in state.items() if key not in INTERNAL]


def open_alerts(settings: Any) -> list[dict[str, Any]]:
    """Every alert that is open now, whatever its route (Settings -> Alerts lists them)."""
    return _open(SecretStore(settings.secrets_dir, SECRETS).get(STATE) or {})


def for_portal(settings: Any) -> dict[str, list[dict[str, Any]]]:
    """What the portal shows: open alerts and recent events of the kinds routed to it."""
    state = SecretStore(settings.secrets_dir, SECRETS).get(STATE) or {}
    shown = {kind for kind, channels in routes(settings).items() if "portal" in channels}
    opened = [a for a in _open(state) if a.get("kind", "health") in shown]
    recent = [
        e
        for e in state.get(RECENT, [])
        if e.get("kind") in shown
        and not (e.get("kind") == "job" and re.search(r"\bfor icloud\b", e.get("text", ""), re.I))
    ]
    tr = i18n.for_settings(settings)
    recent = [
        e
        if e.get("kind") != "job" or "detail" in e
        else {
            **e,
            "text": tr("Your data couldn't be updated. Check Data → Sources or try again later."),
            "detail": e.get("text", ""),
        }
        for e in recent
    ]
    return {
        "open": opened,
        "recent": recent,
        "ambiguous": [
            {"id": key, "at": record["at"], "status": record["status"]}
            for key, record in state.get(DELIVERIES, {}).items()
            if record["status"] in ("attempting", "ambiguous")
        ],
    }


def portal_url(rt: Any, page: str = "#/dashboard") -> str:
    """The portal address of ``page`` on the tailnet, or "" without a public URL."""
    url = rt.settings.public_url
    host = url.split("//", 1)[-1] if url else None
    page = page if page.startswith("#/") else "#/dashboard"
    return f"https://{host}:{rt.settings.portal.get('port', 8447)}/{page}" if host else ""


def _send_with_html(send, subject, text, channel, rt, records, link):
    """The mail with its HTML part; a sender that takes no HTML gets plain text."""
    import inspect

    from vibesynergy import mail_html

    try:
        takes_html = "html" in inspect.signature(send).parameters
    except (TypeError, ValueError):
        takes_html = False
    if not takes_html:
        return send(subject, text, channel)
    tr = i18n.for_settings(rt.settings)
    page = mail_html.render(records, tr, title=subject, link=portal_url(rt, link))
    return send(subject, text, channel, html=page)


def _footer(rt: Any, link: str = "#/dashboard") -> str:
    portal = portal_url(rt, link)
    return f"\n\n— VibeSynergy{f' · {portal} (tailnet)' if portal else ''}"


# Channels ---------------------------------------------------------------------------------


def channels(settings: Any, engine: Any = None) -> dict[str, bool]:
    """The external channels and whether each can send now. Push needs the database (a
    subscribed device), so it is only listed when ``engine`` is given."""
    cfg = settings.alerts
    out = {"email": bool(cfg.get("email_to") and mailer.configured(settings))}
    if engine is not None:
        from vibesynergy import webpush

        try:
            out["push"] = bool(webpush.subscriptions(engine))
        except Exception:  # an old schema without the table: no push
            out["push"] = False
    return out


def deliver(
    rt: Any, subject: str, body: str, only: list[str] | None = None, html: str | None = None
) -> list[str]:
    """Send on every configured channel (or those in ``only``); returns the channels used.
    A failure is logged, not raised."""
    settings = rt.settings
    used = []
    ready = {k: v and (only is None or k in only) for k, v in channels(settings, rt.engine).items()}
    if ready["email"]:
        try:
            extra = {"html": html} if html else {}
            mailer.send(settings, settings.alerts["email_to"], subject, body, **extra)
            used.append("email")
        except mailer.MailError:
            log.exception("sending the alert email failed")
    if ready.get("push"):
        from vibesynergy import webpush

        try:
            if webpush.send_all(rt, subject, body, url="/#/inbox"):
                used.append("push")
        except Exception:  # a push problem never stops the other channels
            log.exception("sending the alert push failed")
    if not used:
        log.warning("alert not sent: no alert channel configured (%s)", subject)
    return used
