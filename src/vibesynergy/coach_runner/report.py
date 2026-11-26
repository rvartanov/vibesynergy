"""The report mail: the coach entry's text, a subject like "Coach weekreview - zo 1 nov" (with
an en dash), and a link to the report in the portal where the athlete can reply or approve a
proposal.

A daily advice is named after the day it is for, not the day of its run (#138), and all its
words come from one view (``query.advice_view``, #147): the evening run of Friday sends
"Advies voor morgen - za 21 nov"; a later version "Advies voor vandaag bijgewerkt - za 21
nov" with "Bijgewerkt 13:48, na je rit". The notice, the push, the mail, the report page, the
Coach page and the start page say the same. Texts are cut on a sentence or word boundary,
with "…" (``core.text.clip``). Only a new day's advice or a real change is pushed or mailed
(``advice_view.notable``); a version that only reports is quiet.

Sent through the existing alert channel (``alerts.deliver``: ``[mail]`` and the alert
address from Settings → Alerts). The subject and the line around the link follow the portal
language; with "follow the browser" they are Dutch, like the coach.

Everything sent is traceable (#115): ``send`` reads the entry back from the database by its
id and sends only that stored text, linked to exactly that entry
(``#/coach/report/<id>``). An entry that is not stored is not sent; a daily advice
version that is no longer the current one for its day (replaced or invalid) is not sent
either. The notice and the push carry the entry id and the excerpt of the entry they
show, which the consistency check (``query.coach_consistency``) holds against the entry.

Outside the contact window (``coach_runner.contact``, #119) a report is not sent but held,
and goes out when the window opens.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from vibesynergy import alerts, i18n
from vibesynergy.core.text import clip
from vibesynergy.core.user_datetime import format_user_short_day
from vibesynergy.portal import prefs

log = logging.getLogger(__name__)

# The other reports, named after the day of their run. A daily advice (morning, evening,
# checkin, injury) is named after the day it is for: ``daily_advice.label``.
SUBJECTS = {
    "weekly": "Coach week review \u2013 {day}",
    "monthly": "Coach month report \u2013 {day}",
    "block": "Coach block review \u2013 {day}",
    "race": "Coach race brief \u2013 {day}",
    "test": "Coach test evaluation \u2013 {day}",
    "thresholds": "Coach threshold check \u2013 {day}",
    "kb": "Coach knowledge base care \u2013 {day}",
}
SHORT_LIMIT = 240  # a push or a notice: one or two lines


def language(settings) -> str:
    chosen = prefs.load(settings.data_dir).get("language")
    return chosen if chosen in i18n.LANGUAGES else "nl"


def subject(settings, job: str, day: date) -> str:
    """The subject of a report that is no daily advice: its task and the day of its run."""
    lang = language(settings)
    tr = i18n.translator(lang)
    return tr(SUBJECTS[job], day=format_user_short_day(day, lang))


def advice_view(rt, entry: dict[str, Any], target: date, tr) -> dict[str, Any] | None:
    """The one view of a daily advice (#147): its title, short text and colour state."""
    from vibesynergy.query import advice_view as view_q

    return view_q.view(rt, entry, tr)


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


def _verdict_lines(view: dict[str, Any] | None) -> list[str]:
    """The colour and its meaning ("Oranje - Doe het wat rustiger aan"), or "Gedaan voor
    vandaag" with the recovery line; then when and why this version came."""
    if not view:
        return []
    return [line for line in (view["verdict"], view["recovery"], view["stamp"]) if line]


def body(rt, entry: dict[str, Any], view: dict[str, Any] | None = None) -> str:
    tr = i18n.translator(language(rt.settings))
    text = (entry.get("text") or "").strip()
    if lines := _verdict_lines(view):
        text = "\n".join(lines) + f"\n\n{text}"
    link = alerts.portal_url(rt, f"#/coach/report/{entry['id']}")
    footer = tr("Reply or approve a proposal in VibeSynergy: {link}", link=link) if link else ""
    return f"{text}\n\n{footer}\n\n— VibeSynergy" if footer else f"{text}\n\n— VibeSynergy"


def html(rt, title: str, entry: dict[str, Any], view: dict[str, Any] | None = None) -> str:
    """The same layout as the morning mail: the note, a chip per session and a button
    to the report. The coach's details (evidence, rule ids, figures) stay out of the mail:
    the athlete found them unreadable there (7-11); the report page keeps them collapsed."""
    from vibesynergy import mail_html

    meta = entry.get("meta") or {}
    verdict = None
    if view and view["state"] != "none":
        verdict = {
            "level": view["colour"],
            "state": view["state"],
            "headline": view["headline"],
            "recovery": view["recovery"],
            "stamp": view["stamp"],
        }
    record = {
        "kind": "daily_coach",
        "rich": {
            "text": (entry.get("text") or "").strip(),
            "sessions": meta.get("sessions") or [],
            "verdict": verdict,
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
    nothing and only points the day's portal notice at the new version. Outside the
    contact window (``contact``, #119) nothing is sent: the report waits in the outbox,
    unless it is the urgent safety exception."""
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
    target = advice_day or (
        daily_advice.target_day(job, day) if job in daily_advice.ADVICE_JOBS else day
    )
    if not by_mail and not quiet:
        from vibesynergy.coach_runner import contact

        # The contact window (#119): outside it the entry stands, its delivery waits;
        # only today's red advice from the athlete's own report goes out at once.
        urgent = advice_job and contact.urgent(rt, entry, target)
        if not contact.allowed(rt, urgent=urgent):
            contact.hold_report(
                rt,
                job=job,
                day=day,
                entry_id=int(entry["id"]),
                advice_day=target if advice_job else None,
            )
            log.info("coach report %s waits for the contact window", entry["id"])
            return []
    tr = i18n.translator(language(rt.settings))
    # A daily advice is named after the day it is for (#138), in its one view (#147);
    # another report after its run.
    view = advice_view(rt, entry, target, tr) if advice_job else None
    title = view["heading"] if view else subject(rt.settings, job, day)
    if advice_job and not by_mail:
        # A daily advice goes where Settings → Alerts sends the kind "advice": by default
        # a push and a notice in the portal, no mail (the owner, 18-11-2026).
        # One notice and one push per target day (#108): a new version replaces the old
        # ones. The notice stores which entry it is about (#115); its text is made from
        # that entry when it is read (#147), so it never shows an old wording.
        return alerts.notify_advice(
            rt,
            key=f"advice:{target.isoformat()}",
            advice_day=target.isoformat(),
            version=entry["id"],
            entry_id=entry["id"],
            excerpt=excerpt(entry),
            tag=f"advice-{target.isoformat()}",
            title=title,
            short=view["push"]["body"] if view else clip(excerpt(entry), SHORT_LIMIT),
            notice=view["notice"] if view else None,
            body=body(rt, entry, view),
            html=html(rt, title, entry, view),
            link=report_link(entry["id"]),
            quiet=quiet,
            state=view["state"] if view else None,
        )
    if quiet:
        return []
    return alerts.deliver(
        rt,
        title,
        body(rt, entry, view),
        only=["email"],
        html=html(rt, title, entry, view),
    )


def short(entry: dict[str, Any], view: dict[str, Any] | None) -> str:
    """One or two lines for a push: the view's push body, else the coach's summary."""
    return view["push"]["body"] if view else clip(excerpt(entry), SHORT_LIMIT)
