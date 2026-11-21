"""One view of a daily advice, for every place that shows it (#147).

The banner, the push, the mail, Meldingen, the start page, the Coach page, the report page
and the MCP all read an advice through ``view``. None of them puts its own title, label or
short text together any more, so they cannot drift apart, and a change of wording reaches
old notices too: a notice stores which entry it is about, its text is made here when it is
read.

One concept: "Advice for <day>", one per day. Its versions say when and why they came:
"Updated 13:48, after your ride". The why comes from what the harness stored with the
version (``meta.harness.changes`` and ``trigger``), in the athlete's words; the runner's
own words (task names, "run", version numbers) stay out of the text.

The colour follows the day. Before or around the session the advice shows its colour and
what that means. Once the day's planned sessions are done and the AI coach's version says
so (it marks them done, skipped or missed, and none is still to keep or adjust), the label
is "Done for today" and the recovery colour moves to a small line beside it. The colour,
its reasons and the sessions' decisions all come from the stored entry (its safety block
and ``meta.sessions``); the plan only says what was planned for the day and what was done.

When to notify (``notable``): a new day's advice, or a version that changes something the
athlete acts on: another colour, a stricter safety level, or a session that is now kept,
adjusted, dropped or proposed differently. A version that only reports (a session now
done or missed) or rewords is quiet: the notice points at it, nothing is pushed or mailed.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.text import clip
from vibesynergy.core.timeutil import today_in
from vibesynergy.i18n import ENGLISH, Translator

log = logging.getLogger(__name__)

# The short name of an advice, said from today: the start of a notice ("Advice for today
# updated at 13:48 …"). The day label (``daily_advice.DAY_LABELS``) adds the date.
SHORT_LABELS = {
    "today": "Advice for today",
    "tomorrow": "Advice for tomorrow",
    "yesterday": "Advice for yesterday",
    "other": "Advice for {day}",
}

# Why a version came, in the athlete's words. The keys are the harness's change kinds
# (``coach_runner.triggers.KINDS``), grouped; a night is one thing to the athlete.
WHY = {
    "injury": "after your pain report",
    "note": "after your note",
    "activity": "after your workout",
    "checkin": "after your check-in",
    "night": "your night is in",
    "plan": "plan changed",
    "reply": "after your reply",
    "manual": "on your request",
    "signal": "after a new signal",
    "evening": "evening preview",
    "morning": "morning update",
}
_CHANGE_GROUP = {"sleep": "night", "hrv": "night", "sleep_check": "night"}
_WHY_ORDER = ("injury", "note", "activity", "checkin", "night", "plan", "reply")

DONE = "Done for today"
# Decisions that report what happened rather than ask for something (#147): a version
# that only moves a session to one of these is no news.
REPORTING = frozenset({"done", "missed"})
# Decisions that still ask something of the athlete today.
OPEN = frozenset({"keep", "adjust", "proposal"})
CLOSED = frozenset({"done", "missed", "skip"})

NOTICE_LIMIT = 200  # a banner: one or two lines
PUSH_LIMIT = 240
REASON_LIMIT = 140


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _norm(title: Any) -> str:
    """A session title without its emoji and punctuation, for matching the plan."""
    return " ".join(re.sub(r"[^\w]+", " ", str(title or "")).split()).casefold()


def _local(entry: dict[str, Any], timezone: str) -> datetime | None:
    at = entry.get("at")
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at)
        except ValueError:
            return None
    if not isinstance(at, datetime):
        return None
    return _aware(at).astimezone(ZoneInfo(timezone))


def _which(day: date, today: date) -> str:
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get((day - today).days, "other")


def short_label(day: date, today: date, tr: Translator = ENGLISH) -> str:
    from vibesynergy.core.user_datetime import format_user_short_day

    return tr(SHORT_LABELS[_which(day, today)], day=format_user_short_day(day, tr.lang))


def _when(moment: datetime, today: date, tr: Translator) -> str:
    """ "13:48", or "yesterday 21:46" / "Fri 20 Nov 21:46" when it was another day."""
    from vibesynergy.core.user_datetime import format_user_short_day

    time = moment.strftime("%H:%M")
    shift = (today - moment.date()).days
    if shift == 0:
        return time
    if shift == 1:
        return tr("yesterday {time}", time=time)
    return f"{format_user_short_day(moment.date(), tr.lang)} {time}"


# --- why a version came ---------------------------------------------------------------------


def why_keys(entry: dict[str, Any], *, first: bool) -> list[str]:
    """The reasons a version came, as keys of ``WHY``, at most two: what came in before it
    (the harness's ``changes``), else how it was started. A first version says only when
    it is the evening preview."""
    from vibesynergy.query import daily_advice

    harness = (entry.get("meta") or {}).get("harness") or {}
    evening = daily_advice.entry_job(entry) == "evening" or entry.get("entry_type") == "evening"
    if first:
        return ["evening"] if evening else []
    found = {_CHANGE_GROUP.get(c, c) for c in harness.get("changes") or []}
    keys = [k for k in _WHY_ORDER if k in found]
    if keys:
        return keys[:2]
    trigger = harness.get("trigger")
    if trigger in ("manual", "signal"):
        return [trigger]
    if evening:
        return ["evening"]
    if trigger == "schedule":
        return ["morning"]
    return []


def why_text(keys: list[str], tr: Translator = ENGLISH) -> str:
    """The reasons as one phrase: "after your check-in", "your night is in and plan changed"."""
    words = [tr(WHY[k]) for k in keys]
    if len(words) > 1:
        return tr("{a} and {b}", a=words[0], b=words[1])
    return words[0] if words else ""


# --- the day state ----------------------------------------------------------------------------


def planned_today(rt, day: date, today: date) -> list[dict[str, Any]] | None:
    """The day's planned trainings with their status (``query.plan``), for today only;
    ``None`` for another day or when the plan cannot be read."""
    if day != today:
        return None
    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import plan as plan_q

    try:
        found = plan_q.plan(
            rt.engine, Window(day, day), today, include_calendar=False, include_sync_status=False
        )
    except Exception:
        log.debug("reading the plan for the advice view failed", exc_info=True)
        return None
    return [
        {"title": i.get("title"), "sport": i.get("sport"), "status": i.get("status")}
        for i in found.get("today") or []
    ]


def _decisions(entry: dict[str, Any]) -> dict[str, str]:
    sessions = (entry.get("meta") or {}).get("sessions") or []
    return {
        _norm(s.get("title")): str(s.get("decision") or "")
        for s in sessions
        if isinstance(s, dict) and s.get("title")
    }


def day_done(entry: dict[str, Any], planned: list[dict[str, Any]] | None) -> bool:
    """The day's planned sessions are over and the version says so: it marks each one done,
    skipped or missed (or the plan has it done and the version asks nothing more of it),
    at least one is done by its own word, and none is still to keep, adjust or propose.
    A rest day, or a day without the plan, is never "done"."""
    if not planned:
        return False
    decisions = _decisions(entry)
    marked_done = False
    for item in planned:
        decision = decisions.get(_norm(item["title"]))
        if decision in OPEN:
            return False
        if decision == "done":
            marked_done = True
        elif decision in CLOSED:
            continue
        elif item.get("status") not in ("done", "changed"):
            return False
    return marked_done


def plan_done(planned: list[dict[str, Any]] | None) -> bool:
    """Every planned training of the day is done by the plan's own match."""
    return bool(planned) and all(i.get("status") in ("done", "changed") for i in planned)


# --- when to notify --------------------------------------------------------------------------


def notable(older: list[dict[str, Any]], entry: dict[str, Any]) -> list[str]:
    """Why a version is worth a push or a mail, or ``[]`` when it is quiet. ``older`` are
    the day's earlier valid versions, newest first. A first version always is ("new");
    a later one when the colour differs from the version before, the safety level is
    stricter, or a session's decision changes to something the athlete acts on (each
    session held against the newest earlier version that names it). A session that is
    now done or missed, or one that is no longer named, is no news."""
    from vibesynergy.query import daily_advice, safety_floor

    if not older:
        return ["new"]
    before = daily_advice.ai_safety(older[0].get("meta")) or {}
    after = daily_advice.ai_safety(entry.get("meta")) or {}
    out = []
    if before.get("colour") != after.get("colour"):
        out.append("colour")
    was, now = str(before.get("level") or ""), str(after.get("level") or "")
    if (
        was in safety_floor.LEVELS
        and now in safety_floor.LEVELS
        and safety_floor.rank(now) > safety_floor.rank(was)
    ):
        out.append("safety_level")
    known: dict[str, str] = {}
    for version in reversed(older):  # oldest first, so the newest mention wins
        known.update(_decisions(version))
    for title, decision in _decisions(entry).items():
        if decision in REPORTING or decision == known.get(title):
            continue
        if title in known or decision != "keep":
            out.append("sessions")
            break
    return out


# --- the view ---------------------------------------------------------------------------------


def view(
    rt,
    entry: dict[str, Any],
    tr: Translator = ENGLISH,
    *,
    today: date | None = None,
    planned: list[dict[str, Any]] | bool | None = False,
    listed: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The one view of an advice entry, or ``None`` for an entry that is no daily advice.

    ``{"entry_id", "date", "label", "short", "heading", "stamp", "why", "time", "first",
    "state", "colour", "colour_name", "headline", "verdict", "recovery", "notice", "push",
    "link", "version", "of", "status", "current_id", "earlier", "updated"}``:

    - ``label`` "Advice for today - Sat 21 Nov" names the day it is for, said from today;
    - ``stamp`` "Updated 13:48, after your ride" (or "Written 21:46, evening preview")
      says when and why this version came; ``why`` is the reason alone;
    - ``state`` is ``colour`` (the colour and its meaning: ``verdict`` "Amber - Take it a
      bit easier"), ``done`` (``verdict`` "Done for today" and ``recovery`` "Recovery:
      amber - <the first stored reason>") or ``none`` (an invalid version);
    - ``first`` is the coach's first line; ``notice`` the banner line and ``push`` the
      push (title, body), all cut cleanly.

    ``planned`` is the day's plan when the caller already read it (``planned_today``),
    ``listed`` the day's versions (``daily_advice.versions``)."""
    from vibesynergy.core.user_datetime import format_user_short_day
    from vibesynergy.query import daily_advice

    day = daily_advice.advice_day(entry)
    if day is None:
        return None
    tz = rt.settings.timezone
    today = today or today_in(tz)
    entry = dict(entry)
    ident = int(entry["id"])
    second = daily_advice.is_second_opinion(entry)
    if second:
        listed = []
    elif listed is None:
        listed = daily_advice.versions(daily_advice._entries(rt, day), tr)
    mine = next((v for v in listed if v["id"] == ident), None)
    current = next((v["id"] for v in listed if v["status"] == "current"), None)
    valid = [v for v in listed if v["valid"]]
    first = mine is None or bool(mine.get("first")) or not mine.get("valid")
    if planned is False:
        planned = planned_today(rt, day, today)
    safety = daily_advice.ai_safety(entry.get("meta"))
    colour = (safety or {}).get("colour")
    done = safety is not None and day_done(entry, planned)  # type: ignore[arg-type]
    state = "none" if safety is None else "done" if done else "colour"

    label = daily_advice.day_label(day, today, tr)
    short = short_label(day, today, tr)
    moment = _local(entry, tz)
    when = _when(moment, today, tr) if moment else ""
    keys = why_keys(entry, first=first)
    why = why_text(keys, tr)
    if first:
        stamp = tr("Written {time}", time=when) if when else ""
    else:
        stamp = tr("Updated {time}", time=when) if when else tr("Updated")
    if stamp and why:
        stamp = f"{stamp}, {why}"

    colour_name = daily_advice.colour_name(colour, tr)
    headline = tr(daily_advice.HEADLINES[colour]) if colour in daily_advice.HEADLINES else ""
    recovery = ""
    if state == "done":
        verdict = tr(DONE)
        reasons = [str(r).strip() for r in (safety or {}).get("reasons") or [] if str(r).strip()]
        line = tr("Recovery: {colour}", colour=colour_name.lower())
        recovery = f"{line} \u2013 {clip(reasons[0], REASON_LIMIT)}" if reasons else line
    elif state == "colour":
        verdict = f"{colour_name} \u2013 {headline}"
    else:
        verdict = ""

    opening = daily_advice._summary(entry)
    if first:
        notice = f"{label}: {opening}"
        heading = label
    else:
        updated = tr("{advice} updated at {time}", advice=short, time=when) if when else short
        notice = f"{updated}, {why}: {opening}" if why else f"{updated}: {opening}"
        heading = tr(
            "{advice} updated \u2013 {day}",
            advice=short,
            day=format_user_short_day(day, tr.lang),
        )
    push_body = ". ".join(p.rstrip(". ") for p in (stamp if not first else "", verdict) if p)
    push_body = f"{push_body}. {opening}" if push_body else opening
    return {
        "entry_id": ident,
        "date": day.isoformat(),
        "label": label,
        "short": short,
        "heading": heading,
        "stamp": stamp,
        "why": why,
        "time": when,
        "first": opening,
        "state": state,
        "colour": colour,
        "colour_name": colour_name,
        "headline": tr(DONE) if state == "done" else headline,
        "verdict": verdict,
        "recovery": recovery,
        "notice": clip(notice, NOTICE_LIMIT),
        "push": {"title": heading, "body": clip(push_body, PUSH_LIMIT)},
        "link": f"#/coach/report/{ident}",
        "version": (mine or {}).get("version"),
        "of": len(valid) or None,
        "status": (mine or {}).get("status"),
        "current_id": current,
        "earlier": max(0, len(listed) - 1) if mine else 0,
        "updated": not first,
        "second_opinion": second,
    }


def for_entry_id(
    rt, entry_id: Any, tr: Translator = ENGLISH, **kwargs: Any
) -> dict[str, Any] | None:
    """``view`` of a stored entry by its id; ``None`` when it is not stored or no advice."""
    from vibesynergy.coach_runner import report

    entry = report.stored(rt, entry_id)
    return view(rt, entry, tr, **kwargs) if entry else None


def views(rt, entries: list[dict[str, Any]], tr: Translator = ENGLISH) -> dict[int, dict]:
    """``view`` for many entries (a feed), reading each day's versions and plan once."""
    from vibesynergy.query import daily_advice

    today = today_in(rt.settings.timezone)
    days: dict[date, tuple[list, Any]] = {}
    out: dict[int, dict] = {}
    for entry in entries:
        day = daily_advice.advice_day(entry)
        if day is None:
            continue
        if day not in days:
            days[day] = (
                daily_advice.versions(daily_advice._entries(rt, day), tr),
                planned_today(rt, day, today),
            )
        listed, planned = days[day]
        found = view(rt, entry, tr, today=today, planned=planned, listed=listed)
        if found is not None:
            out[int(entry["id"])] = found
    return out
