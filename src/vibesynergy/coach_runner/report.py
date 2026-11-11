"""The report mail: the coach entry's text, a subject like "Coach ochtend - zo 1 nov" (with
an en dash), and a link to the report in the portal where the athlete can reply or approve a
proposal.

Sent through the existing alert channel (``alerts.deliver``: ``[mail]`` and the alert
address from Settings → Alerts). The subject and the line around the link follow the portal
language; with "follow the browser" they are Dutch, like the coach.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from vibesynergy import alerts, i18n
from vibesynergy.core.user_datetime import format_user_short_day
from vibesynergy.portal import prefs

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


def body(rt, entry: dict[str, Any]) -> str:
    tr = i18n.translator(language(rt.settings))
    text = (entry.get("text") or "").strip()
    link = alerts.portal_url(rt, f"#/coach/report/{entry['id']}")
    footer = tr("Reply or approve a proposal in VibeSynergy: {link}", link=link) if link else ""
    return f"{text}\n\n{footer}\n\n— VibeSynergy" if footer else f"{text}\n\n— VibeSynergy"


def html(rt, title: str, entry: dict[str, Any]) -> str:
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
        },
    }
    tr = i18n.translator(language(rt.settings))
    link = alerts.portal_url(rt, f"#/coach/report/{entry['id']}")
    return mail_html.render([record], tr, title=title, link=link)


def send(rt, job: str, day: date, entry: dict[str, Any]) -> list[str]:
    """Mail the report; returns the channels used (empty when mail isn't set up)."""
    title = subject(rt.settings, job, day)
    return alerts.deliver(rt, title, body(rt, entry), only=["email"], html=html(rt, title, entry))
