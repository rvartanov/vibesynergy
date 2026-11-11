"""Compact coach reads: one day, and one Monday-to-Sunday week.

The portal and these tools share ``dashboard``, ``plan``, ``daily_coach`` and the diary.
The payload stays small enough that "how should I train today?" is one call.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.core.user_datetime import format_user_date
from vibesynergy.mcp.identity import current_connection
from vibesynergy.query import coach, coach_workouts, dashboard, journal, week_progress, workouts
from vibesynergy.query import plan as plan_q
from vibesynergy.query.daily_coach import fixed_safety_summary
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import activity_unified, coach_decision_log

_RECOVERY = (
    ("hrv.night_avg", "hrv"),
    ("hr.resting", "resting_hr"),
    ("sleep.duration", "sleep"),
    ("recovery.readiness", "readiness"),
)


def today_brief(rt: Runtime, day: date) -> dict[str, Any]:
    """What a coach needs for one day. Dates in ``date`` stay ISO; ``date_label`` is for people."""
    engine = rt.engine
    board = dashboard.dashboard(
        engine,
        day,
        timezone=rt.settings.timezone,
        athlete=rt.settings.athlete,
        sources=rt.registry.enabled_ids(),
        gear=workouts.gear(engine, rt.raw, day)["gear"],
        rt=rt,
    )
    diary = journal.day_journal(engine, day)
    checkin = diary.get("checkin") or "missing"
    if isinstance(checkin, dict) and checkin.get("note"):
        checkin = {**checkin, "note": str(checkin["note"])[:280]}
    today_items = [_item(row) for row in board["plan"]["today"]]
    tomorrow = day + timedelta(days=1)
    if day.weekday() == 6:
        # The dashboard week stops on Sunday. Monday is still tomorrow.
        nxt = plan_q.plan(engine, Window(tomorrow, tomorrow), day)
        tomorrow_rows = [
            row
            for row in nxt["items"]
            if row["date"] == str(tomorrow) and row["kind"] in plan_q.TRAINING_KINDS
        ]
    else:
        tomorrow_rows = [row for row in board["plan"]["week"] if row["date"] == str(tomorrow)]
    tomorrow_items = [_item(row) for row in tomorrow_rows]
    race = board.get("next_race")
    phase = (board.get("plan_phase") or {}).get("phase")
    if race:
        race = {**race, "phase": phase}
    supporting = board.get("supporting") or []
    level = board["safety"]["level"]
    safety_text = ""
    if level in ("flag", "refuse"):
        safety_text = fixed_safety_summary(rt, "morning", level, board["safety"].get("rules"))
    warnings = [item["message"] for item in board["attention"] if item.get("area") == "system"]
    verdict = board["verdict"]
    return {
        "date": str(day),
        "date_label": format_user_date(day),
        "checkin": checkin,
        "complaints": [
            {
                "title": item["title"],
                "pain": item.get("latest_pain"),
                "status": item["status"],
            }
            for item in board["injuries"]
        ],
        "pain_today": [
            {
                "title": item["title"],
                "pain": (item.get("pain") or {}).get("pain"),
            }
            for item in diary["injuries"]
            if item.get("pain")
        ],
        "recovery": {
            name: _recovery_row(board["recovery"].get(metric)) for metric, name in _RECOVERY
        },
        "load": {
            "form": board["load"].get("form"),
            "tsb": board["load"].get("tsb"),
            "acwr": board["load"].get("acwr"),
            "ctl": board["load"].get("ctl"),
            "ramp": board["load"].get("ctl_ramp_7d"),
        },
        "planned_today": today_items[0] if today_items else None,
        "horizon": _horizon(rt, day),
        "today": today_items,
        "plan_source": board["plan"].get("plan_source"),
        "not_in_plan": [
            _item(row) for row in board["plan"].get("not_in_plan") or [] if row["date"] == str(day)
        ],
        "tomorrow": tomorrow_items,
        "done_recent": _recent(engine, day),
        "race": race,
        "b_race": supporting[0] if supporting else None,
        "verdict": {
            "level": verdict["level"],
            "headline": verdict["headline"],
            "advice": verdict["advice"],
            "reasons": verdict["reasons"],
        },
        "attention": [
            {"level": item["level"], "area": item["area"], "message": item["message"]}
            for item in board["attention"][:12]
        ],
        "safety": {"level": level, "rules": board["safety"]["rules"], "text": safety_text},
        "coach_entries": _coach_entries(rt),
        "warnings": warnings,
    }


def week_brief(rt: Runtime, start: date, end: date, today: date) -> dict[str, Any]:
    """Monday to Sunday, planned against what was done, plus open proposals."""
    calendar = plan_q.plan(rt.engine, Window(start, end), today)
    sessions = [
        _week_session(item) for item in calendar["items"] if item["kind"] in plan_q.TRAINING_KINDS
    ]
    weeks = calendar.get("weeks") or []
    this_week = next((w for w in weeks if w["start"] == str(start)), None)
    progress = week_progress.week_progress(rt.engine, start, today, plan=calendar, raw=rt.raw)
    # The targets' time done is the dashboard's (8-11): a training you gave that is in the
    # plan counts. The plan layer left all coaching out, so the coach said "running an hour
    # short" while the dashboard said on target.
    by_family = {s["family"]: s for s in (progress or {}).get("sports") or []}
    targets = []
    for target in (this_week or {}).get("targets") or []:
        same = by_family.get(target.get("family"))
        if same is not None:
            target = {
                **target,
                "done_s": same["done_s"],
                "done_distance_m": same.get("done_distance_m"),
                # The goal without optional sessions, as the dashboard measures it.
                "goal_s": same.get("goal_s"),
                "optional_s": same.get("optional_s"),
            }
        targets.append(target)
    return {
        "start": str(start),
        "end": str(end),
        "plan_source": calendar.get("plan_source"),
        "items": sessions,
        "not_in_plan": [_week_session(item) for item in calendar.get("not_in_plan") or []],
        "totals": _totals(sessions),
        "note": (this_week or {}).get("note"),
        "targets": targets,
        "race": calendar.get("next_race"),
        # Per sport: target, done, still planned; per workout planned against actual.
        "progress": progress,
    }


def _item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{row.get('source')}:{row.get('source_id')}",
        "source": row.get("source"),
        "in_plan": row.get("in_plan", True),
        "title": row.get("title"),
        "sport": row.get("sport"),
        "date": row.get("date"),
        "owner": row.get("ownership"),
        "editable": bool(row.get("write_authority")),
        "status": row.get("status"),
        "intensity": plan_q.planned_intensity(row),
        "distance_m": plan_q.planned_distance_m(row),
        "duration_s": plan_q.planned_duration_s(row),
    }


def _week_session(item: dict[str, Any]) -> dict[str, Any]:
    match = (item.get("done_by") or [None])[0]
    extra = item.get("extra") or {}
    ascent = extra.get("ascent_m")
    if ascent is None:
        ascent = extra.get("elevationGain")
    matched = None
    if match:
        matched = {
            "id": match.get("id"),
            "name": match.get("name"),
            "duration_s": match.get("moving_s") or match.get("elapsed_s"),
            "distance_m": match.get("distance_m"),
            "ascent_m": match.get("ascent_m"),
        }
    return {
        "id": f"{item.get('source')}:{item.get('source_id')}",
        "source": item.get("source"),
        "in_plan": item.get("in_plan", True),
        "date": item.get("date"),
        "title": item.get("title"),
        "sport": item.get("sport"),
        "duration_s": plan_q.planned_duration_s(item),
        "distance_m": plan_q.planned_distance_m(item),
        "ascent_m": ascent,
        "intensity": plan_q.planned_intensity(item),
        "status": item.get("status"),
        "matched": matched,
    }


def _totals(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    def _sum(key: str, actual: bool) -> float | None:
        total = 0.0
        seen = False
        for session in sessions:
            if actual:
                if session["status"] not in ("done", "changed") or not session.get("matched"):
                    continue
                value = (session["matched"] or {}).get(key)
            else:
                value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
                seen = True
        if not seen:
            return None
        return round(total, 1)

    return {
        "sessions": len(sessions),
        "done": sum(session["status"] == "done" for session in sessions),
        "changed": sum(session["status"] == "changed" for session in sessions),
        "missed": sum(session["status"] == "missed" for session in sessions),
        "distance_m": {
            "planned": _sum("distance_m", False),
            "completed": _sum("distance_m", True),
        },
        "duration_s": {
            "planned": _sum("duration_s", False),
            "completed": _sum("duration_s", True),
        },
        "ascent_m": {"planned": _sum("ascent_m", False), "completed": _sum("ascent_m", True)},
    }


def _recent(engine: Engine, day: date) -> list[dict[str, Any]]:
    start = day - timedelta(days=2)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.local_date,
                activity_unified.c.name,
                activity_unified.c.sport,
                activity_unified.c.distance_m,
                activity_unified.c.moving_s,
                activity_unified.c.ascent_m,
                activity_unified.c.group_id,
            )
            .where(
                activity_unified.c.local_date.between(start, day),
                activity_unified.c.kind.is_distinct_from("coaching"),
            )
            .order_by(activity_unified.c.local_date.desc())
            .limit(12)
        ).all()
    return [
        {
            "id": group_id,
            "date": str(local_date),
            "name": name,
            "sport": sport,
            "distance_m": distance_m,
            "duration_s": moving_s,
            "ascent_m": ascent_m,
        }
        for local_date, name, sport, distance_m, moving_s, ascent_m, group_id in rows
    ]


def _usual_words(block: dict[str, Any] | None) -> str | None:
    if not block or block.get("baseline_std") is None or block.get("value") is None:
        return None
    low = round((block["baseline_mean"] - block["baseline_std"]) * 10) / 10
    high = round((block["baseline_mean"] + block["baseline_std"]) * 10) / 10
    value = round(block["value"] * 10) / 10
    if value < low:
        return "below usual"
    if value > high:
        return "above usual"
    return "usual"


def _recovery_row(block: dict[str, Any] | None) -> dict[str, Any]:
    if not block:
        return {"words": None, "value": None, "usual": None}
    return {
        "words": _usual_words(block),
        "value": block.get("value"),
        "usual": block.get("baseline_mean"),
    }


RUNNER_AUTHOR = "client:coach-runner"  # coach_runner.runner.RUNNER_AUTHOR


def _coach_entries(rt: Runtime) -> list[dict[str, Any]]:
    author = current_connection().author
    with rt.engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                select(coach_decision_log)
                .where(
                    coach_decision_log.c.subject_id == 1,
                    # The coach is one role with two doors: over MCP (the caller) and the API
                    # runner inside VibeSynergy (11-11: an MCP coach missed the runner's reports).
                    coach_decision_log.c.author.in_((author, RUNNER_AUTHOR)),
                    coach_decision_log.c.entry_type.in_(tuple(coach_workouts.COACH_ENTRY_TYPES)),
                )
                .order_by(coach_decision_log.c.id.desc())
                .limit(3)
            ).mappings()
        ]
    if not rows:
        return []
    snap = coach.snapshot(rt.engine, timezone=rt.settings.timezone)
    out = []
    for row in rows:
        entry = coach_workouts.coach_row(row)
        text = entry.get("text") or ""
        summary, _, caveat = text.partition("\n")
        out.append(
            {
                "entry_type": entry.get("entry_type"),
                "for_date": entry.get("for_date"),
                "summary": summary[:400],
                "caveat": caveat[:200] or None,
                "freshness": coach_workouts.coach_entry_freshness(entry, snap, rt),
                "at": entry.get("at"),
            }
        )
    return out


def _horizon(rt: Runtime, day: date) -> dict[str, Any] | None:
    """The plan horizon guard for the coach: missing or doubled sessions on Garmin."""
    from vibesynergy.query import horizon

    return horizon.for_runtime(rt, day)
