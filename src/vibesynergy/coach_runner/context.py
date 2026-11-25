"""Preloaded context for harness tasks (layer 3).

Every task gets the fixed base (``registry.BASE_CONTEXT``) plus its own chain. Each block is
built from ``vibesynergy.query`` only, has a character budget and a version, and never fails
the run: a block that cannot be built says so in one line and the model can still fetch it
through its tools.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from vibesynergy.coach_runner import recent_words, registry
from vibesynergy.core.timeutil import Window

log = logging.getLogger(__name__)

# Bump when the chain format or a base block changes shape (2: the safety block, #88;
# 3: replies carry their id and the daily advice gets ``coach_team``, #118; 4: the daily
# advice gets ``day_thread``, #115; 5: the safety block's ``sleep_check`` is the night's
# sleep check, #100; 6: the insight gets ``insight_rewrite`` and the activity's notes are
# marked context only, #114; 7: the athlete's own words of the last days in ``recent_words``
# for every advice task and the insight, and today's check-in note only there, #170).
VERSION = 7


@dataclass(frozen=True)
class Block:
    name: str
    title: str
    budget: int  # characters of JSON
    build: Callable[[Any, date, dict[str, Any]], Any]
    version: int = 1
    # False: the block fits itself to its budget and is never cut afterwards, because a cut
    # could drop what must stay (the athlete's own words in ``activity``, #158).
    cut: bool = True


DESCRIPTION_CHARS = 700  # a session's step text in a block; the tools have the rest


def _short(text: Any) -> Any:
    if isinstance(text, str) and len(text) > DESCRIPTION_CHARS:
        return text[:DESCRIPTION_CHARS] + " …"
    return text


def _volume(found: dict[str, Any]) -> list[dict[str, Any]]:
    """Volume buckets, compact: hours and km per period and sport. A multisport holder is
    left out, its parts count, so a brick is not counted twice."""
    from vibesynergy.query.multisport import is_multisport

    return [
        {
            "period": b.get("period"),
            "sport": b.get("sport"),
            "n": b.get("count"),
            "h": round((b.get("moving_s") or 0) / 3600, 2),
            "km": round((b.get("distance_m") or 0) / 1000, 1),
        }
        for b in found.get("buckets") or []
        if not is_multisport(b.get("sport"))
    ]


def _series_ends(points: Any) -> Any:
    """A daily series as where it started and ended in the window, and how many points."""
    if not isinstance(points, list) or not points:
        return points
    first, last = points[0], points[-1]
    return {"from": first, "to": last, "n": len(points)}


def _brief(rt, day: date, cache: dict[str, Any]) -> dict[str, Any]:
    if "brief" not in cache:
        from vibesynergy.query import coach_brief

        cache["brief"] = coach_brief.today_brief(rt, day)
    return cache["brief"]


def _athlete(rt, day, cache):
    from vibesynergy.query import coach

    return coach.athlete_for_coach(rt.engine, rt.settings)


def _communication(rt, day, cache):
    return (
        "Advies eerst, gewone coachtaal, hoogstens één kanttekening, cijfers vertaald naar "
        "betekenis, kort. Bronnen en herkomst alleen intern."
    )


def _injury_log(rt, day) -> list[dict[str, Any]]:
    from vibesynergy.query import journal

    return [
        {
            "title": row.get("title"),
            "status": row.get("status"),
            "latest_pain": row.get("latest_pain"),
            "warnings": row.get("warnings"),
        }
        for row in journal.injuries(rt.engine, day, days=14)
    ]


def _safety(rt, day, cache):
    """The safety outcome every task starts from (#88): what the fixed rules decided and
    why, the night's measurement gaps, and what the athlete reported. Each part stands on
    its own: an empty injury log does not hide a floor that fired, and a part that cannot
    be built says so instead of dropping the rest."""

    def part(build):
        try:
            return build()
        except Exception as exc:
            log.warning("safety context part failed: %s", exc)
            return f"niet beschikbaar ({type(exc).__name__}); haal op met vibesynergy_today"

    injuries = part(lambda: _injury_log(rt, day))
    if cache.get("task") == "insight":
        # The insight writes a public line (INS-2): it keeps the injury log it always had
        # and gets no check-in, measurements or floor.
        return {"injuries": injuries}
    brief = part(lambda: _brief(rt, day, cache))
    if not isinstance(brief, dict):
        return {"floor": brief, "injuries": injuries}
    safety = brief.get("safety") or {}
    checkin = _checkin(brief.get("checkin"), cache)
    return {
        "floor": {
            "level": safety.get("level"),
            "colour": safety.get("colour"),
            "rules": safety.get("rules") or [],
            "reasons": safety.get("reasons") or [],
            "notes": safety.get("notes") or [],
        },
        # The night's sleep check (#100): the mark, the reasons, the metrics that do not
        # count and the athlete's own choice (override), which beats the check.
        "sleep_check": _sleep_check(brief.get("sleep_check")),
        "measurement": brief.get("measurement"),
        "checkin": checkin,
        "complaints": brief.get("complaints") or [],
        "pain_today": brief.get("pain_today") or [],
        "injuries": injuries,
    }


NOTE_IN_RECENT_WORDS = "volledig in blok recent_words"


def _checkin(found: Any, cache: dict[str, Any]) -> Any:
    """The check-in; with ``recent_words`` in the run its note is only there, in full (#170)."""
    if isinstance(found, dict) and found.get("note") and recent_words.in_chain(cache):
        return {**found, "note": NOTE_IN_RECENT_WORDS}
    return found


def _sleep_check(found: Any) -> dict[str, Any] | None:
    if not isinstance(found, dict):
        return None
    keys = ("date", "state", "auto_state", "override", "metrics", "not_counted_metrics")
    out = {k: found.get(k) for k in keys}
    out["reasons"] = found.get("reasons") or []
    return out


def _memory(rt, day, cache):
    from vibesynergy.query import coach

    active = coach.state(rt.engine, "context").get("active") or {}
    return [
        {"kind": row.get("kind"), "text": row.get("text"), "days": row.get("days") or []}
        for row in (active.get("payload") or {}).get("constraints") or []
        if row.get("active", True)
    ]


def _today(rt, day, cache):
    brief = _brief(rt, day, cache)
    keys = ("date", "checkin", "complaints", "pain_today", "load", "today", "tomorrow")
    out = {k: brief.get(k) for k in keys} | {"race": brief.get("race")}
    out["checkin"] = _checkin(out["checkin"], cache)
    return out


def _recovery(rt, day, cache):
    return _brief(rt, day, cache).get("recovery")


def _plan_horizon(rt, day, cache):
    from vibesynergy.query import plan_model

    found = plan_model.view(rt.engine, day, day + timedelta(days=8))
    return {
        "version": found.get("version"),
        "sessions": [
            {
                **{k: s.get(k) for k in ("date", "sport", "name", "status")},
                "description": _short(s.get("description")),
            }
            for s in found.get("sessions") or []
        ],
        "weeks": [
            {k: w.get(k) for k in ("monday", "phase", "note")} for w in found.get("weeks") or []
        ],
        "guard": _brief(rt, day, cache).get("horizon"),
    }


def _plan_next_week(rt, day, cache):
    from vibesynergy.query import plan_model

    monday = day + timedelta(days=7 - day.weekday())
    found = plan_model.view(rt.engine, monday, monday + timedelta(days=6))
    return [
        {k: s.get(k) for k in ("date", "sport", "name", "status")}
        for s in found.get("sessions") or []
    ]


def _week(rt, day, cache):
    from vibesynergy.query import coach_brief

    monday = day - timedelta(days=day.weekday())
    found = coach_brief.week_brief(rt, monday, monday + timedelta(days=6), day)
    found.pop("progress", None)  # per-day detail; the items and totals carry the week
    return found


def _weather(rt, day, cache):
    from vibesynergy.query import activity_weather

    found = activity_weather.forecast(rt)
    found["days"] = (found.get("days") or [])[:9]  # the plan horizon
    return found


def _replies(rt, day, cache):
    from vibesynergy.query import coach_replies

    return [
        {"reply_id": r["id"], "entry_id": r["entry_id"], "at": r["at"], "text": r["text"]}
        for r in coach_replies.unread(rt.engine)
    ]


def _coach_team(rt, day, cache):
    """More than one AI coach (#118): the advisory AI coaches' second opinions for the day
    this run's advice is for, with where each differs from the current advice, and the
    plan proposals that wait for the athlete or were decided in the last two days."""
    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import daily_advice, plan_proposals
    from vibesynergy.store.schema import plan_proposal

    target = daily_advice.target_day(str(cache.get("task") or ""), day)
    opinions = [
        {
            "from": "adviserende AI-coach",
            "entry_id": o["id"],
            "at": o["at"],
            "advice": str(o["advice"] or "")[:400],
            "safety_level": o["safety_level"],
            "reasons": o["reasons"],
            "sessions": o["sessions"],
            "differs_from_your_advice": o["differences"],
        }
        for o in daily_advice.thread_opinions(rt, target)
    ]
    since = utc_now() - timedelta(hours=plan_proposals.APPROVAL_HOURS)
    with rt.engine.connect() as conn:
        rows = conn.execute(
            plan_proposal.select()
            .where(
                (plan_proposal.c.status == plan_proposals.OPEN)
                | (plan_proposal.c.decided_at >= since)
            )
            .order_by(plan_proposal.c.id)
        ).mappings()
        proposals = [
            {
                "approval_id": f"proposal:{r['id']}",
                "status": r["status"],
                "by": "jij" if r["role"] == "primary" else "adviserende AI-coach",
                "change": r["summary"],
                "reason": str(r["reason"])[:200],
            }
            for r in rows
        ]
    if not opinions and not proposals:
        return None
    return {"second_opinions": opinions, "plan_proposals": proposals}


THREAD_TEXT_CHARS = 400


def _by(version: dict[str, Any]) -> str:
    """Who wrote a version, in the words of the thread."""
    if version.get("via") == "runner":
        return "AI-coach in VibeSynergy"
    if version.get("role") == "own":
        return "eigen agent van de atleet"
    return "AI-coach via MCP"


def _day_thread(rt, day, cache):
    """The day thread (#115): every earlier version of the advice for the day this run's
    advice is for, oldest first, with what changed and why and the athlete's replies on
    each; for the evening run also the day it reviews. ``current`` is the version a new
    one replaces: cite its id in ``changed_because``."""
    from vibesynergy.query import daily_advice

    target = daily_advice.target_day(str(cache.get("task") or ""), day)
    out = []
    for when in sorted({day, target}):
        found = daily_advice.thread(rt, when)
        versions = [
            {
                "id": v["id"],
                "version": v["version"],
                "status": {"current": "huidig", "replaced": "vervangen", "invalid": "ongeldig"}[
                    v["status"]
                ],
                "at": v["at"],
                "by": _by(v),
                "colour": v["colour"],
                "safety_level": v["safety_level"],
                "advice": str(v["advice"] or "")[:THREAD_TEXT_CHARS],
                "sessions": v["sessions"],
                "changed_because": (v.get("changed_because") or {}).get("reason"),
                "new_data_before_it": v["changes"],
                "replies": [
                    {"reply_id": r["reply_id"], "at": r["at"], "text": r["text"][:300]}
                    for r in v["replies"]
                ],
            }
            for v in found["versions"]
        ]
        current = next((v["id"] for v in versions if v["status"] == "huidig"), None)
        out.append(
            {
                "advice_for": when.isoformat(),
                "this_run": when == target,
                "current": current,
                "versions": versions,
            }
        )
    if not any(d["versions"] for d in out):
        return "nog geen advies voor deze dag: jouw entry is de eerste versie"
    return out


def _recent_reports(rt, day, cache):
    from vibesynergy.query import predictions

    rows = predictions.coach_entries(rt.engine, Window(day - timedelta(days=6), day))
    return [
        {
            "for_date": r.get("for_date"),
            "entry_type": r.get("entry_type"),
            "text": str(r.get("text") or "")[:400],
        }
        for r in rows[:6]
    ]


def _fitness(rt, day, cache):
    from vibesynergy.query import training

    found = training.fitness(rt.engine, Window(day - timedelta(days=27), day))
    return {
        group: {name: _series_ends(points) for name, points in (values or {}).items()}
        for group, values in found.items()
    }


def _month_bounds(day: date) -> tuple[date, date, date]:
    """The month the report is about (the one before ``day``'s), and the one before that."""
    end = day.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    before = (start - timedelta(days=1)).replace(day=1)
    return before, start, end


def _month(rt, day, cache):
    from vibesynergy.query import training

    before, start, end = _month_bounds(day)
    return {
        "month": start.strftime("%Y-%m"),
        "this": _volume(training.volume(rt.engine, Window(start, end), granularity="month")),
        "previous": _volume(
            training.volume(
                rt.engine, Window(before, start - timedelta(days=1)), granularity="month"
            )
        ),
    }


def _block(rt, day, cache):
    """The phase that ends: its weeks with targets, the volume done per week, and the
    first weeks of the next phase."""
    from vibesynergy.query import plan_model, training

    found = plan_model.view(rt.engine, day - timedelta(days=7 * 16), day + timedelta(days=21))
    weeks = sorted(found.get("weeks") or [], key=lambda w: w["monday"])
    past = [w for w in weeks if w["monday"] <= day.isoformat()]
    if not past:
        return None
    phase = past[-1].get("phase")
    block = []
    for w in reversed(past):
        if w.get("phase") != phase:
            break
        block.insert(0, w)
    start = date.fromisoformat(block[0]["monday"])
    return {
        "phase": phase,
        "weeks": [
            {k: w.get(k) for k in ("monday", "kind", "hours", "note", "targets")} for w in block
        ],
        "done": _volume(training.volume(rt.engine, Window(start, day), granularity="week")),
        "next": [
            {k: w.get(k) for k in ("monday", "phase", "kind", "hours", "note")}
            for w in weeks
            if w["monday"] > day.isoformat()
        ],
    }


def _threshold_signals(rt, day, cache):
    from vibesynergy.query import thresholds

    return {
        "signals": thresholds.signals(rt.engine, day),
        "current": thresholds.current_values(rt.engine),
    }


KB_PER_MONTH = 5


def _kb_due(rt, day, cache):
    """The articles longest without an update (five a month) and what is already pending."""
    from vibesynergy.query import knowledge

    active = [a for a in knowledge.list_articles(rt.engine) if a["status"] == "active"]
    due = sorted(active, key=lambda a: a.get("updated_at") or "")[:KB_PER_MONTH]
    return {
        "check": due,
        "pending": [
            {k: p.get(k) for k in ("id", "kind", "target", "summary")}
            for p in knowledge.pending_proposals(rt.engine)
        ],
        "articles": len(active),
    }


def _races(rt, day, cache):
    from vibesynergy.query import plan

    return plan.calendar_races(rt.engine, day)


ACTIVITY_BUDGET = 8000  # characters for the whole block: a brick of up to five activities
MAX_ACTIVITIES = 5  # a brick: the holder and its parts

# The summary numbers of the merged activity, as stored (seconds, metres, bpm, watts).
_SUMMARY = (
    "sport",
    "local_date",
    "start_utc",
    "elapsed_s",
    "moving_s",
    "distance_m",
    "ascent_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "norm_power",
    "training_load",
    "purpose",
)


def own_text(description: Any) -> str | None:
    """What the athlete wrote in a provider description, without VibeSynergy's block
    (``workout_texts.own_text``)."""
    from vibesynergy.query import workout_texts

    return workout_texts.own_text(description)


def _number(value: Any, digits: int = 0) -> Any:
    if isinstance(value, float):
        return round(value, digits) if digits else round(value)
    return value


PRIVATE_IS = "context only: never quote it in a public text"


def _own_words(detail: dict[str, Any], stored: dict[str, str] | None = None) -> dict[str, Any]:
    """The athlete's own words for one activity.

    When VibeSynergy holds texts of its own for the workout (``stored``, #114): its name,
    public text and private text, plus the linked notes that are not already in them. The
    private text is context only and never goes into a public text.

    Otherwise: per source the title and the description without VibeSynergy's block, and the
    linked notes (text only). A text that is already there (the Strava description the
    diary imported as a note) is given once."""
    from vibesynergy.query import workout_texts

    seen: set[str] = set()
    if stored:
        out: dict[str, Any] = {}
        for field in workout_texts.FIELDS:
            text = (stored.get(field) or "").strip()
            if field == "name":
                if text:
                    out["name"] = text
                continue
            new = workout_texts.unseen(text, seen)
            if new:
                out[field] = new
        if "private" in out:
            out["private_is"] = PRIVATE_IS
        notes = []
        for found in detail.get("notes") or []:
            new = workout_texts.unseen(str(found.get("text") or ""), seen)
            if new:
                notes.append(new)
        if notes:
            out["notes"] = notes
            out["notes_are"] = PRIVATE_IS
        return out
    sources = []
    for member in detail.get("sources") or []:
        if member.get("removed_at"):
            continue
        extra = member.get("extra") if isinstance(member.get("extra"), dict) else {}
        text = own_text(extra.get("description"))
        row: dict[str, Any] = {"source": str(member.get("id") or "").split(":", 1)[0]}
        if member.get("name"):
            row["title"] = member["name"]
        if text and text not in seen:
            row["description"] = text
            seen.add(text)
        sources.append(row)
    notes = []
    for found in detail.get("notes") or []:
        text = str(found.get("text") or "").strip()
        if text and text not in seen:
            notes.append(text)
            seen.add(text)
    out = {"sources": sources, "notes": notes}
    if notes:
        out["notes_are"] = PRIVATE_IS
    return out


def _stored_texts(rt, group: str) -> dict[str, str]:
    from vibesynergy.query import workout_texts

    try:
        if not workout_texts.stored(rt.engine, group):
            return {}
        # A field VibeSynergy holds no version of yet still follows from the sources.
        found = workout_texts.texts(rt.engine, group)
        return {f: found[f]["text"] for f in workout_texts.FIELDS if found[f]["text"]}
    except Exception as exc:  # the block goes on with the descriptions and notes
        log.warning("workout texts for the activity block failed: %s", exc)
        return {}


def _effort(rt, group: str) -> dict[str, Any] | None:
    from vibesynergy.query import activity_effort

    try:
        found = activity_effort.for_activity(rt.engine, rt.raw, group)
    except Exception as exc:  # the block goes on without it
        log.warning("effort for the activity block failed: %s", exc)
        return None
    out = {
        field: {k: found[field].get(k) for k in ("value", "label", "source")}
        for field in activity_effort.FIELDS
        if (found.get(field) or {}).get("value") is not None
    }
    return out or None


def _lap(lap: dict[str, Any]) -> dict[str, Any]:
    units = lap.get("sport_units") or {}
    extra = lap.get("extra") if isinstance(lap.get("extra"), dict) else {}
    row = {
        "i": lap.get("lap_index"),
        "duration": units.get("duration"),
        "distance": units.get("distance"),
        "avg_hr": _number(lap.get("avg_hr")),
        "avg_power": _number(lap.get("avg_power")),
        "pace_or_speed": units.get("pace_or_speed"),
        "intensity": extra.get("intensityType"),
    }
    return {k: v for k, v in row.items() if v is not None}


def _zones(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for z in rows:
        out.setdefault(str(z.get("kind")), []).append(
            {"zone": z.get("zone"), "s": _number(z.get("seconds")), "from": _number(z.get("low"))}
        )
    return out


def activity_view(rt, activity_id: str) -> dict[str, Any]:
    """One activity for the AI coach, compact and purpose-built (#158): the athlete's own
    words first, then the summary numbers, effort, laps and time in zones. No provider
    ``extra``, no account, profile or owner fields."""
    from vibesynergy.query import views

    detail = views.activity_detail(rt.engine, rt.parquet, activity_id)
    unified = detail.get("activity") or {}
    group = str(unified.get("group_id") or activity_id)
    summary = {k: _number(unified.get(k)) for k in _SUMMARY if unified.get(k) is not None}
    units = {k: v for k, v in (unified.get("sport_units") or {}).items() if k != "family"}
    laps = [_lap(lap) for lap in detail.get("laps") or []]
    out: dict[str, Any] = {
        "id": group,
        "own_words": _own_words(detail, _stored_texts(rt, group)),
        "summary": summary | units,
        # Where the session was, for the workout name of an outdoor workout (#188).
        "place": _place(rt, group),
        "effort": _effort(rt, group),
        "laps": laps,
        "zones": _zones(detail.get("zones") or []),
    }
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _place(rt, group: str) -> str | None:
    from vibesynergy.query import activity_weather

    try:
        return activity_weather.place(rt, group)
    except Exception:  # the place is a nicety; the view goes without it
        return None


def _size(data: Any) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=str))


def fit_activities(found: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """The views within ``budget`` characters of JSON. What gives way, in order: the laps
    (from the end, a count stays), then the zones, the effort and last the summary. The
    athlete's own words are never cut."""
    out = [dict(v) for v in found]
    totals = {i: len(v.get("laps") or []) for i, v in enumerate(out)}
    keep = max(totals.values(), default=0)
    while _size(out) > budget and keep > 0:
        keep = keep // 2 if keep > 4 else keep - 1
        for i, view in enumerate(out):
            if totals[i] > keep:
                view["laps"] = (found[i].get("laps") or [])[:keep]
                view["laps_total"] = totals[i]
    for key in ("zones", "effort", "summary"):
        if _size(out) <= budget:
            break
        for view in out:
            view.pop(key, None)
    return out


def _recent_words(rt, day, cache):
    return recent_words.build(rt, day, cache)


def _activity(rt, day, cache):
    ids = cache.get("activity_ids") or []
    found = [activity_view(rt, str(i)) for i in ids[:MAX_ACTIVITIES]]
    return fit_activities(found, ACTIVITY_BUDGET) or None


def _plan_match(rt, day, cache):
    """The day's planned sessions with what was done for each (#116): ``planned`` and
    ``done`` lines, the content ``check`` and the ``deviation``. A session that is
    ``changed`` (another workout, or another size or intensity) was not done as prescribed."""
    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import plan as plan_q
    from vibesynergy.query import plan_match, plan_model

    found = plan_model.view(rt.engine, day, day)
    try:
        calendar = plan_q.plan(
            rt.engine, Window(day, day), day, include_calendar=False, include_sync_status=False
        )
        matched = {i.get("session_id"): i for i in calendar["items"] if i.get("session_id")}
    except Exception as exc:  # the sessions still go in, without what was done
        log.warning("plan match context failed: %s", exc)
        matched = {}
    out = []
    for s in found.get("sessions") or []:
        row = {
            **{k: s.get(k) for k in ("session_id", "sport", "name", "status")},
            "description": _short(s.get("description")),
        }
        item = matched.get(s.get("session_id"))
        if item is not None:
            row["match"] = item.get("status")
            row |= plan_match.for_coach(item)
        out.append(row)
    return out


REWRITE_CORRECTIONS = 3  # the newest corrections the rewrite gets


def _insight_rewrite(rt, day, cache):
    """Per activity of the run that already has a public insight (#114, phase 4): that
    insight, which of the athlete's texts changed after it was written, and the athlete's
    corrections of it (private, newest first). None for a first insight."""
    from vibesynergy.coach_runner import insight_rewrite
    from vibesynergy.query import publication_insight, workout_texts

    out = []
    for activity_id in (cache.get("activity_ids") or [])[:MAX_ACTIVITIES]:
        current = publication_insight.for_activity(rt.engine, str(activity_id))
        if current is None:
            continue
        written = insight_rewrite._when(current["at"])
        versions = workout_texts.history(rt.engine, str(activity_id))
        later = [
            v
            for v in versions
            if v["kind"] in ("edit", "restore")
            and v["source"] != "ai"
            and (written is None or (insight_rewrite._when(v["at"]) or written) > written)
        ]
        row: dict[str, Any] = {
            "id": str(activity_id),
            "current_insight": current["text"],
            "written_at": current["at"],
        }
        changed = sorted({v["field"] for v in later if v["field"] in workout_texts.FIELDS})
        if changed:
            row["texts_changed_after_it"] = changed
        fixes = [
            {"at": v["at"], "text": v["text"], "new": v in later}
            for v in workout_texts.corrections(rt.engine, str(activity_id))
        ][:REWRITE_CORRECTIONS]
        if fixes:
            row["athlete_corrections"] = fixes
            row["corrections_are"] = (
                "private: what the athlete says is not right in the insight; take it into "
                "account in the new insight, never quote it"
            )
        out.append(row)
    return out or None


BLOCKS: dict[str, Block] = {
    b.name: b
    for b in (
        Block("athlete", "Atleet", 3000, _athlete),
        Block("communication_rule", "Communicatieregel", 400, _communication),
        Block(
            "safety",
            "Veiligheid: regeluitkomst, slaapcheck, meetgaten, check-in, klachten, blessures",
            4000,
            _safety,
            version=4,
        ),
        Block("coach_memory", "Coachgeheugen", 2000, _memory),
        # Version 3 (#170): with ``recent_words`` in the run the check-in note is there.
        Block("today", "Vandaag", 3000, _today, version=3),
        Block("recovery", "Herstel", 1200, _recovery),
        # Version 2 (#153): Garmin workouts and events that are not VibeSynergy's own
        # (Garmin Coach, added by hand) are gone from the guard and the week.
        Block("plan_horizon", "Plan komende 9 dagen", 10000, _plan_horizon, version=2),
        Block("plan_next_week", "Plan volgende week", 2500, _plan_next_week),
        Block("week", "Deze week", 6000, _week, version=2),
        Block("weather", "Weer", 2000, _weather),
        Block("replies", "Antwoorden op eerdere rapporten", 3000, _replies, version=2),
        Block("recent_reports", "Recente rapporten", 3000, _recent_reports),
        Block(
            "day_thread",
            "Dagdraad: de eerdere versies van het advies en de antwoorden, oudste eerst",
            6000,
            _day_thread,
        ),
        Block(
            "coach_team",
            "Second opinions van adviserende AI-coaches en planvoorstellen",
            3000,
            _coach_team,
        ),
        Block("fitness", "Fitheid", 2500, _fitness),
        # Version 2 (#158): a compact view, the athlete's own words first; no raw provider
        # data, so the Strava description and the notes are never cut off.
        Block(
            "activity",
            "Activiteit: eerst de eigen titel, beschrijving en notities van de atleet, "
            "dan de cijfers",
            ACTIVITY_BUDGET,
            _activity,
            version=2,
            cut=False,
        ),
        # #170: the moving context. Fits itself to its budget by compressing older days, so
        # it is never cut afterwards.
        Block(
            "recent_words",
            "Eigen woorden van de atleet, afgelopen dagen: check-in-notities, klachten, "
            "notities en per workout de eigen naam, openbare en privétekst",
            recent_words.BUDGET,
            _recent_words,
            cut=False,
        ),
        Block("plan_match", "Gepland en gedaan op die dag", 2500, _plan_match, version=2),
        # #114, phase 4: an insight written again after the texts changed or the athlete
        # corrected it. Self-describing, so the insight prompt needs no change.
        Block(
            "insight_rewrite",
            "Opnieuw schrijven: het huidige publieke inzicht, welke eigen teksten de atleet "
            "daarna veranderde, en zijn correcties op het inzicht (privé: verwerk wat hij "
            "rechtzet in het nieuwe inzicht, citeer het niet)",
            2500,
            _insight_rewrite,
        ),
        Block("month", "Vorige maand", 4000, _month),
        # Version 2 (#111): VibeSynergy's own races; the primary race is the next A race.
        Block("races", "Wedstrijden", 1500, _races, version=2),
        Block("kb_due", "Kennisbank: te controleren", 3000, _kb_due),
        Block("threshold_signals", "Drempelsignalen", 4000, _threshold_signals),
        Block("block", "Blok dat eindigt", 6000, _block),
    )
}


def _render(block: Block, data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    if block.cut and len(text) > block.budget:
        text = text[: block.budget] + " …(ingekort; haal de rest op met de tools)"
    return text


def preload(rt, task_id: str, day: date, **extra: Any) -> dict[str, Any]:
    """The task's context, block by block: ``{"text", "blocks": [{name, version, chars, ok}]}``.

    Unknown chain names (a block not built yet) and failing blocks are listed, not raised.
    """
    cache: dict[str, Any] = {**extra, "task": task_id}
    parts: list[str] = []
    report: list[dict[str, Any]] = []
    for name in registry.chain_for(task_id):
        block = BLOCKS.get(name)
        if block is None:
            report.append({"name": name, "version": None, "chars": 0, "ok": False})
            continue
        try:
            data = block.build(rt, day, cache)
            ok = True
        except Exception as exc:  # one block never sinks the run
            log.warning("context block %s failed: %s", name, exc)
            data = f"Niet beschikbaar ({type(exc).__name__}); gebruik de tools."
            ok = False
        if data in (None, [], {}):
            data = "geen"
        text = _render(block, data)
        parts.append(
            f'<context name="{name}" v="{block.version}">\n{block.title}: {text}\n</context>'
        )
        report.append({"name": name, "version": block.version, "chars": len(text), "ok": ok})
    return {"version": VERSION, "text": "\n".join(parts), "blocks": report}
