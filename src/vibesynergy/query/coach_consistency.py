"""The continuity check (#115): what the AI coach said, what was sent and what the athlete
sees must be one thread. Deterministic, read only, no model.

It holds the stored day thread (``daily_advice.thread``) against the portal notices
(``alerts``) for yesterday, today and tomorrow, and reports each gap as a finding in the
shape of the consistency layers (#61/#62): ``{level, where, rule, evidence, fix}``.

The rules:

* ``notice_without_entry``: a notice (kind ``advice`` or ``daily_coach``) that names no
  stored entry, or one that no longer exists. Nothing may be sent that is not stored.
* ``notice_text_not_in_entry``: the text a notice shows is not in the entry it links to.
* ``notice_day_differs`` (#138): an advice notice that names another day than the day its
  entry is for: its key or ``advice_day``, or the day in its text (an evening advice
  labelled with the day of its run).
* ``notice_replaced``: the day's advice notice points at a version that is no longer the
  current one.
* ``entry_without_notice``: the day's current advice has no notice, while notices go to
  the portal.
* ``contradiction``: a later version that changed the colour, the safety level or a
  session decision without ``changed_because``. ``no_reference``: a later version that
  names no earlier one at all. Only versions written under the harness that requires it
  (``HARNESS_VERSION`` 3 and up) count, so older entries do not report forever.

``daily`` runs it once a day from the alerts check and keeps the outcome in the audit log
(action ``coach.consistency``), for the weekly harness audit (#62). The Coach page and
``vibesynergy_today`` show the live outcome.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import insert, select

from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.store.schema import audit_log

log = logging.getLogger(__name__)

ACTION = "coach.consistency"
WARNING = "warning"
INFO = "info"
THREAD_HARNESS = 3  # the first harness version that required changed_because

FIXES = {
    "notice_without_entry": "Never send a text that is not stored; the notice is dropped "
    "at the next version.",
    "notice_text_not_in_entry": "Send the stored text of the entry, never a rewritten one.",
    "notice_day_differs": "Name an advice after the day it is for (daily_advice.label), "
    "never the day of its run.",
    "notice_replaced": "Point the day's notice at the current version.",
    "entry_without_notice": "Send the notice for the current version (Coach → Settings → "
    "Send again), or check Settings → Alerts.",
    "contradiction": "A later version says what changes against the earlier one and why "
    "(changed_because).",
    "no_reference": "A later version names the version it replaces (changed_because).",
}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def text_in_entry(shown: Any, entry_text: Any) -> bool:
    """The text a notice shows (its excerpt) is part of the stored entry's text."""
    part = _norm(shown).removesuffix("…").strip()
    return bool(part) and part in _norm(entry_text)


def day_gap(notice: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """How an advice notice names another day than its entry is for (#138), or ``None``:
    the day in its key, its ``advice_day`` and the short day in its text ("za 21 nov", in
    either language) must all be the entry's target day."""
    from vibesynergy.core.user_datetime import format_user_short_day
    from vibesynergy.query import daily_advice

    target = daily_advice.advice_day(entry)
    if target is None:
        return None
    key_day = str(notice.get("key") or "").removeprefix("advice:")
    for name, value in (("key", key_day), ("advice_day", notice.get("advice_day"))):
        if value and value != target.isoformat():
            return f"its {name} names {value}; entry #{entry['id']} is for {target.isoformat()}"
    text = _norm(notice.get("text"))
    named = {_norm(format_user_short_day(target, lang)) for lang in ("en", "nl")}
    if text and not any(day in text for day in named):
        return (
            f"its text “{str(notice.get('text') or '')[:80]}” does not name "
            f"{target.isoformat()}, the day entry #{entry['id']} is for"
        )
    return None


def _finding(rule: str, where: str, evidence: str, level: str = WARNING) -> dict[str, Any]:
    return {"level": level, "where": where, "rule": rule, "evidence": evidence, "fix": FIXES[rule]}


def _notices(rt) -> list[dict[str, Any]]:
    from vibesynergy import alerts
    from vibesynergy.core.secrets import SecretStore

    store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
    advice = list(store.get(alerts.ADVICE_STATE) or [])
    coach = [e for e in (store.get(alerts.STATE) or {}).get(alerts.RECENT, []) or []]
    return advice + [e for e in coach if e.get("kind") == "daily_coach"]


def _sent_day(rt, notice: dict[str, Any]) -> date | None:
    """The local day a notice was sent, or ``None`` when it does not say."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        moment = datetime.fromisoformat(str(notice.get("at")))
        return moment.astimezone(ZoneInfo(rt.settings.timezone)).date()
    except (TypeError, ValueError, KeyError):
        return None


def _portal_gets_advice(rt) -> bool:
    from vibesynergy import alerts

    return "portal" in alerts.routes(rt.settings).get("advice", [])


def check(rt, day: date | None = None) -> dict[str, Any]:
    """The gaps for ``day`` (default today) and the days around it: ``{"days", "findings"}``."""
    from vibesynergy.coach_runner import report
    from vibesynergy.query import daily_advice

    day = day or today_in(rt.settings.timezone)
    days = [day - timedelta(days=1), day, day + timedelta(days=1)]
    wanted = {f"advice:{d.isoformat()}" for d in days}
    findings: list[dict[str, Any]] = []
    notices = _notices(rt)
    by_key: dict[str, dict[str, Any]] = {}
    for notice in notices:
        kind = notice.get("kind")
        key = str(notice.get("key") or "")
        if kind == "advice":
            if key not in wanted:
                continue
            by_key[key] = notice
        elif _sent_day(rt, notice) not in days:
            continue  # a coach note from before the window
        where = f"notice {key}"
        ident = notice.get("entry_id")
        entry = report.stored(rt, ident) if ident is not None else None
        if entry is None:
            findings.append(
                _finding(
                    "notice_without_entry",
                    where,
                    f"the notice “{str(notice.get('text') or '')[:120]}” names "
                    + (f"entry #{ident}, which is not stored" if ident else "no stored entry"),
                )
            )
            continue
        if kind == "advice" and (gap := day_gap(notice, entry)):
            findings.append(_finding("notice_day_differs", where, gap))
        shown = notice.get("excerpt")
        if shown is not None and not text_in_entry(shown, entry.get("text")):
            findings.append(
                _finding(
                    "notice_text_not_in_entry",
                    where,
                    f"“{str(shown)[:120]}” is not in entry #{ident}",
                )
            )
    portal = _portal_gets_advice(rt)
    for when in days:
        found = daily_advice.thread(rt, when)
        listed = found["versions"]  # oldest first
        current = next((v for v in listed if v["status"] == "current"), None)
        key = f"advice:{when.isoformat()}"
        notice = by_key.get(key)
        if current is not None and notice is not None:
            pointed = notice.get("entry_id")
            if pointed is not None and int(pointed) != current["id"]:
                findings.append(
                    _finding(
                        "notice_replaced",
                        f"notice {key}",
                        f"the notice shows advice #{pointed}; the current version is "
                        f"#{current['id']} (version {current['version']})",
                    )
                )
        elif current is not None and portal and when <= day:
            findings.append(
                _finding(
                    "entry_without_notice",
                    f"advice for {when.isoformat()}",
                    f"advice #{current['id']} (version {current['version']}) has no notice",
                    level=INFO,
                )
            )
        findings += _thread_gaps(rt, when, listed)
    return {"days": [d.isoformat() for d in days], "findings": findings}


def _thread_gaps(rt, day: date, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Later versions that do not build on the one before them."""
    from vibesynergy.coach_runner import report

    out = []
    for v in listed:
        if not v["valid"] or (v["version"] or 0) < 2 or v.get("changed_because"):
            continue
        entry = report.stored(rt, v["id"]) or {}
        harness = (entry.get("meta") or {}).get("harness") or {}
        if int(harness.get("version") or 0) < THREAD_HARNESS:
            continue
        where = f"advice #{v['id']} for {day.isoformat()}"
        if v["differences"]:
            fields = ", ".join(d["field"] for d in v["differences"])
            out.append(
                _finding(
                    "contradiction",
                    where,
                    f"version {v['version']} changed {fields} without saying why",
                )
            )
        else:
            out.append(
                _finding(
                    "no_reference",
                    where,
                    f"version {v['version']} names no earlier version",
                    level=INFO,
                )
            )
    return out


def daily(rt, now=None) -> dict[str, Any] | None:
    """Run the check once per local day and keep its outcome in the audit log. Returns
    the outcome when it ran, else ``None`` (it already ran today)."""
    day = today_in(rt.settings.timezone)
    target = f"{ACTION}:{day.isoformat()}"
    with rt.engine.connect() as conn:
        seen = conn.scalar(
            select(audit_log.c.id)
            .where(audit_log.c.action == ACTION, audit_log.c.target == target)
            .limit(1)
        )
    if seen is not None:
        return None
    found = check(rt, day)
    with rt.engine.begin() as conn:
        conn.execute(
            insert(audit_log).values(
                at=now or utc_now(),
                actor="coach_consistency",
                action=ACTION,
                target=target,
                outcome="gaps" if found["findings"] else "ok",
                detail=found,
            )
        )
    if found["findings"]:
        log.warning(
            "continuity check %s: %s",
            day,
            ", ".join(f"{f['rule']} ({f['where']})" for f in found["findings"]),
        )
    return found


MESSAGES = {
    "notice_without_entry": "A notice shows a text that is not stored with your AI coach.",
    "notice_text_not_in_entry": "A notice shows a text that is not in the report it links to.",
    "notice_day_differs": "A notice names another day than the advice it links to ({day}).",
    "notice_replaced": "The notice for {day} shows an older version of the advice.",
    "entry_without_notice": "The current advice for {day} got no notice.",
    "contradiction": "A later version of the advice for {day} changed without saying why.",
    "no_reference": "A later version of the advice for {day} names no earlier version.",
}


def for_page(rt, tr) -> list[dict[str, Any]]:
    """The live findings in the athlete's words, for the Coach page."""
    import re

    from vibesynergy.core.user_datetime import format_user_short_day

    out = []
    for f in check(rt)["findings"]:
        found = re.search(r"\d{4}-\d{2}-\d{2}", f["where"] + " " + f["evidence"])
        day = format_user_short_day(date.fromisoformat(found.group(0)), tr.lang) if found else ""
        out.append({**f, "message": tr(MESSAGES[f["rule"]], day=day)})
    return out
