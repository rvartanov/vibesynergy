"""The report mail: the coach entry's text, a subject like "Coach ochtend - zo 1 nov" (with
an en dash), and a link to the report in the portal where the athlete can reply or approve a
proposal.

Sent through the existing alert channel (``alerts.deliver``: ``[mail]`` and the alert
address from Settings → Alerts). The subject and the line around the link follow the portal
language; with "follow the browser" they are Dutch, like the coach.

Everything sent is traceable (#115): ``send`` reads the entry back from the database by its
id and sends only that stored text, linked to exactly that entry
(``#/coach/report/<id>``). An entry that is not stored is not sent; a daily advice
version that is no longer the current one for its day (replaced or invalid) is not sent
either. The notice and the push carry the entry id and the excerpt of the entry they
show, which the consistency check (``query.coach_consistency``) holds against the entry.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from vibesynergy import alerts, i18n
from vibesynergy.core.user_datetime import format_user_short_day
from vibesynergy.portal import prefs

log = logging.getLogger(__name__)

SUBJECTS = {
    "morning": "Coach morning \u2013 {day}",
    "evening": "Coach evening \u2013 {day}",
    "weekly": "Coach week review \u2013 {day}",
    "monthly": "Coach month report \u2013 {day}",
    "block": "Coach block review \u2013 {day}",
    "race": "Coach race brief \u2013 {day}",
    "test": "Coach test evaluation \u2013 {day}",
    "thresholds": "Coach threshold check \u2013 {day}",
    "kb": "Coach knowledge base care \u2013 {day}",
    "injury": "Coach injury check \u2013 {day}",
    "checkin": "Coach on your check-in \u2013 {day}",
}


def language(settings) -> str:
    chosen = prefs.load(settings.data_dir).get("language")
    return chosen if chosen in i18n.LANGUAGES else "nl"


def subject(settings, job: str, day: date) -> str:
    lang = language(settings)
    tr = i18n.translator(lang)
    return tr(SUBJECTS[job], day=format_user_short_day(day, lang))


def advice_for(rt, job: str, day: date, tr) -> dict[str, Any] | None:
    """The day's current advice (``daily_advice``) for a daily advice report, so the mail
    says the same colour and words as the dashboard (#89). ``None`` for other reports."""
    from vibesynergy.query import daily_advice

    if job not in daily_advice.ADVICE_JOBS:
        return None
    try:
        found = daily_advice.current(rt, day, tr=tr, trigger=False)
    except Exception:
        return None
    return found if found.get("level") else None


def _verdict_line(advice: dict[str, Any] | None, tr) -> str:
    if not advice:
        return ""
    from vibesynergy.query import daily_advice

    return f"{daily_advice.colour_name(advice['level'], tr)}: {advice['headline']}"


def body(rt, entry: dict[str, Any], advice: dict[str, Any] | None = None) -> str:
    tr = i18n.translator(language(rt.settings))
    text = (entry.get("text") or "").strip()
    if line := _verdict_line(advice, tr):
        text = f"{line}\n\n{text}"
    link = alerts.portal_url(rt, f"#/coach/report/{entry['id']}")
    footer = tr("Reply or approve a proposal in VibeSynergy: {link}", link=link) if link else ""
    return f"{text}\n\n{footer}\n\n— VibeSynergy" if footer else f"{text}\n\n— VibeSynergy"


def html(rt, title: str, entry: dict[str, Any], advice: dict[str, Any] | None = None) -> str:
    """The same layout as the morning mail: the note, a chip per session and a button
    to the report. The coach's details (evidence, rule ids, figures) stay out of the mail:
    the athlete found them unreadable there (7-11); the report page keeps them collapsed."""
    from vibesynergy import mail_html

    meta = entry.get("meta") or {}
    record = {
        "kind": "daily_coach",
        "rich": {
            "text": (entry.get("text") or "").strip(),
            "sessions": meta.get("sessions") or [],
            "verdict": (
                {"level": advice["level"], "headline": advice["headline"]} if advice else None
            ),
        },
    }
    tr = i18n.translator(language(rt.settings))
    link = alerts.portal_url(rt, f"#/coach/report/{entry['id']}")
    return mail_html.render([record], tr, title=title, link=link)


def stored(rt, entry_id: Any) -> dict[str, Any] | None:
    """The coach entry as stored, or ``None``: what is sent is read back from here."""
    from sqlalchemy import select

    from vibesynergy.store.schema import coach_decision_log

    try:
        ident = int(entry_id)
    except (TypeError, ValueError):
        return None
    with rt.engine.connect() as conn:
        row = (
            conn.execute(select(coach_decision_log).where(coach_decision_log.c.id == ident))
            .mappings()
            .first()
        )
    return dict(row) if row else None


def report_link(entry_id: int) -> str:
    """The page every notice, push and mail about an entry links to."""
    return f"#/coach/report/{int(entry_id)}"


def excerpt(entry: dict[str, Any]) -> str:
    """The part of the entry a push or a notice shows: its first paragraph."""
    return " ".join((entry.get("text") or "").strip().split("\n\n")[0].split())


def send(
    rt,
    job: str,
    day: date,
    entry: dict[str, Any],
    *,
    advice_day: date | None = None,
    by_mail: bool = False,
    quiet: bool = False,
) -> list[str]:
    """Send the report; returns the channels used (empty when none is set up, or when the
    entry may not be sent: not stored, or a replaced version, #115).
    ``advice_day`` is the day the advice is for (the evening review: tomorrow, #90).
    A daily advice follows the alert kind ``advice``; ``by_mail`` (sending a report again
    by hand) always mails it. ``quiet`` (a version that only rewords the advice) sends
    nothing and only points the day's portal notice at the new version."""
    from vibesynergy.query import daily_advice

    found = stored(rt, entry.get("id"))
    if found is None:
        log.warning("coach report %s not sent: the entry is not stored", entry.get("id"))
        return []
    entry = found
    advice_job = job in daily_advice.ADVICE_JOBS
    state = daily_advice.version_state(rt, entry["id"]) if advice_job else None
    if advice_job and state is not None and state["status"] != "current":
        log.warning(
            "coach report %s not sent: it is %s (current: %s)",
            entry["id"],
            state["status"],
            state["current_id"],
        )
        return []
    title = subject(rt.settings, job, day)
    target = advice_day or (
        daily_advice.target_day(job, day) if job in daily_advice.ADVICE_JOBS else day
    )
    tr = i18n.translator(language(rt.settings))
    advice = advice_for(rt, job, target, tr)
    if advice_job and not by_mail:
        # A daily advice goes where Settings → Alerts sends the kind "advice": by default
        # a push and a notice in the portal, no mail (Robert, 18-11-2026).
        # One notice and one push per target day (#108): a new version replaces the old
        # ones; its id is the version, so an older dismissal does not hide it. The notice
        # links to exactly this version (#115).
        update = bool(state and (state["version"] or 0) > 1)
        return alerts.notify_advice(
            rt,
            key=f"advice:{target.isoformat()}",
            version=entry["id"],
            entry_id=entry["id"],
            excerpt=excerpt(entry),
            tag=f"advice-{target.isoformat()}",
            prefix=tr("Updated:") + " " if update else "",
            title=title,
            short=short(entry, advice, tr),
            body=body(rt, entry, advice),
            html=html(rt, title, entry, advice),
            link=report_link(entry["id"]),
            quiet=quiet,
        )
    if quiet:
        return []
    return alerts.deliver(
        rt,
        title,
        body(rt, entry, advice),
        only=["email"],
        html=html(rt, title, entry, advice),
    )


def short(entry: dict[str, Any], advice: dict[str, Any] | None, tr) -> str:
    """One or two lines for a push or a portal notice: the verdict and the coach's summary."""
    summary = excerpt(entry)
    line = _verdict_line(advice, tr)
    text = f"{line}. {summary}" if line and summary else line or summary
    return text[:240]
