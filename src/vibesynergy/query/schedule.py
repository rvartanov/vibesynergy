"""The training calendar: per day what was planned and what was done, per week the
totals against the week's targets. Read-only, built on ``plan.plan`` (plan workouts with
their status and matched activity) plus the activities that were not in the plan.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import plan as plan_q
from vibesynergy.store.schema import activity_unified, daily_unified, plan_link, plan_session

MAX_DAYS = 62  # a month view with the weeks around it
# The day panel's recovery line: what the day looked like, from the best source.
WELLNESS = {
    "recovery.readiness": "readiness",
    "hrv.night_avg": "hrv_ms",
    "sleep.duration": "sleep_s",
    "hr.resting": "rhr_bpm",
    # The rest of Garmin's day view: heart-rate range, stress, Body Battery and steps.
    "hr.max": "hr_max_bpm",
    "recovery.stress.avg": "stress",
    "recovery.body_battery.charged": "bb_charged",
    "recovery.body_battery.drained": "bb_drained",
    "daily.steps": "steps",
}


def calendar(engine: Any, start: date, end: date, today: date, raw: Any = None) -> dict[str, Any]:
    if end < start:
        raise ValueError("end before start")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days")
    view = plan_q.plan(engine, Window(start, end), today, include_sync_status=False)
    # When VibeSynergy leads the plan, planned sessions can be moved and skipped here:
    # each intervals.icu copy maps back to its model session.
    from vibesynergy.query import plan_model

    editable = plan_model.source(engine) == "vibesynergy"
    sessions: dict[str, str] = {}
    if editable:
        with engine.connect() as conn:
            sessions = {
                str(remote): sid
                for sid, remote in conn.execute(
                    select(plan_link.c.session_id, plan_link.c.remote_id)
                    .join(plan_session, plan_session.c.id == plan_link.c.session_id)
                    .where(plan_link.c.target == "intervals", plan_session.c.status == "planned")
                )
            }
    days: dict[str, dict[str, Any]] = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        days[day.isoformat()] = {"date": day.isoformat(), "planned": [], "done": []}
    matched: set[str] = set()
    for item in view["items"]:
        if item.get("kind") not in plan_q.TRAINING_KINDS:
            continue
        key = str(item.get("date"))[:10]
        if key not in days:
            continue
        done = item.get("done_by") or []
        matched.update(str(d.get("id")) for d in done if d.get("id"))
        days[key]["planned"].append(
            {
                "session_id": sessions.get(str((item.get("extra") or {}).get("icu_id"))),
                "title": item.get("title"),
                "sport": item.get("sport"),
                "status": item.get("status"),
                "planned_s": plan_q.planned_duration_s(item),
                "distance_m": plan_q.planned_distance_m(item),
                "description": item.get("description"),
                "brick_part": (item.get("extra") or {}).get("brick_part"),
                "done": [
                    {"id": d.get("id"), "name": d.get("name"), "moving_s": d.get("moving_s")}
                    for d in done
                ],
            }
        )
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.local_date,
                activity_unified.c.sport,
                activity_unified.c.name,
                activity_unified.c.moving_s,
                activity_unified.c.elapsed_s,
                activity_unified.c.distance_m,
                activity_unified.c.training_load,
                activity_unified.c.avg_hr,
                activity_unified.c.start_utc,
                activity_unified.c.sources,
            )
            .where(activity_unified.c.local_date.between(start, end))
            .order_by(activity_unified.c.start_utc)
        ).all()
        wellness = conn.execute(
            select(daily_unified.c.date, daily_unified.c.metric, daily_unified.c.value).where(
                daily_unified.c.date.between(start, end),
                daily_unified.c.metric.in_(list(WELLNESS)),
            )
        ).all()
    for day, metric, value in wellness:
        entry = days.get(day.isoformat())
        if entry is not None:
            entry.setdefault("wellness", {})[WELLNESS[metric]] = value
    stats = {
        r.group_id: {
            "moving_s": r.moving_s or r.elapsed_s,
            "distance_m": r.distance_m,
            "load": r.training_load,
            "avg_hr": r.avg_hr,
        }
        for r in rows
    }
    for entry in days.values():
        for planned in entry["planned"]:
            for done in planned["done"]:
                done.update(
                    {k: v for k, v in stats.get(str(done["id"]), {}).items() if v is not None}
                )
    # Compliance per done planned session (8-11), as in the week progress.
    from vibesynergy.query import activity_effort
    from vibesynergy.query.week_progress import compliance_of

    icu = {}
    if raw is not None:
        groups = [
            {"group_id": r.group_id, "local_date": r.local_date, "start_utc": r.start_utc,
             "sport": r.sport, "moving_s": r.moving_s, "elapsed_s": r.elapsed_s,
             "distance_m": r.distance_m, "sources": r.sources or []}
            for r in rows
        ]  # fmt: skip
        try:
            icu = activity_effort.icu_for_groups(engine, raw, groups)
        except Exception:  # compliance must never break the calendar
            icu = {}
    for entry in days.values():
        for planned in entry["planned"]:
            for done in planned["done"]:
                pct, source = compliance_of(
                    planned.get("planned_s"),
                    done.get("moving_s"),
                    (icu.get(str(done["id"])) or {}).get("compliance"),
                )
                done["compliance"], done["compliance_source"] = pct, source
    # A planned brick is one holder too: the brick run and the day's ride with it.
    for entry in days.values():
        plans = entry["planned"]
        runs = [
            i
            for i, p in enumerate(plans)
            if plan_q.family(p.get("sport")) == "run"
            and (p.get("brick_part") == "loop" or "brick" in (p.get("title") or "").lower())
        ]
        rides = [i for i, p in enumerate(plans) if plan_q.family(p.get("sport")) == "ride"]
        if runs and rides:
            ride = next((i for i in rides if plans[i].get("brick_part") == "fiets"), rides[0])
            entry["planned_multisport"] = [
                {
                    "title": f"{plans[ride].get('title')} + {plans[runs[0]].get('title')}",
                    "parts": [ride, runs[0]],
                    "planned_s": sum(plans[i].get("planned_s") or 0 for i in (ride, runs[0])),
                }
            ]
    # A multisport is one holder with its parts: the parts count, the holder does not.
    from vibesynergy.query import multisport

    holders = multisport.parts(
        [
            {
                "id": r.group_id,
                "sport": r.sport,
                "start_utc": r.start_utc,
                "elapsed_s": r.elapsed_s,
                "moving_s": r.moving_s,
            }
            for r in rows
        ]
    )
    part_of = {part: holder for holder, ids in holders.items() for part in ids}
    for entry in days.values():
        for planned in entry["planned"]:
            for done in planned["done"]:
                if str(done["id"]) in part_of:
                    done["part_of"] = part_of[str(done["id"])]
    per_week: dict[str, float] = defaultdict(float)
    summary: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.group_id in holders:
            days[r.local_date.isoformat()].setdefault("multisport", []).append(
                {
                    "id": r.group_id,
                    "name": r.name,
                    "sport": r.sport,
                    "moving_s": r.elapsed_s or r.moving_s,
                    "load": r.training_load,
                    "parts": holders[r.group_id],
                }
            )
            continue
        seconds = r.moving_s or r.elapsed_s or 0
        monday = r.local_date - timedelta(days=r.local_date.weekday())
        per_week[monday.isoformat()] += seconds
        week = summary.setdefault(
            monday.isoformat(), {"time_s": 0.0, "load": None, "count": 0, "sports": {}}
        )
        week["time_s"] += seconds
        if r.training_load is not None:  # unknown stays unknown: never a zero
            week["load"] = (week["load"] or 0.0) + r.training_load
        week["count"] += 1
        fam = plan_q.family(r.sport) or "other"
        sport = week["sports"].setdefault(fam, {"time_s": 0.0, "distance_m": 0.0})
        sport["time_s"] += seconds
        sport["distance_m"] += r.distance_m or 0
        if r.group_id in matched:
            continue
        days[r.local_date.isoformat()]["done"].append(
            {
                "id": r.group_id,
                "part_of": part_of.get(r.group_id),
                "name": r.name,
                "sport": r.sport,
                "moving_s": seconds,
                "distance_m": r.distance_m,
                "load": r.training_load,
                "avg_hr": r.avg_hr,
            }
        )
    # A brick done as a separate ride and run (no Garmin multisport) is one holder too, when
    # both planned parts were done; it adds nothing to the totals.
    for key, entry in days.items():
        for brick in entry.get("planned_multisport") or []:
            done_ids = []
            for i in brick["parts"]:
                found = [str(d["id"]) for d in entry["planned"][i]["done"] if d.get("id")]
                done_ids += found[:1]
            if len(done_ids) < 2 or any(d in part_of for d in done_ids):
                continue
            entry.setdefault("multisport", []).append(
                {
                    "id": None,
                    "name": brick["title"],
                    "sport": "brick",
                    "moving_s": sum(stats.get(d, {}).get("moving_s") or 0 for d in done_ids),
                    "load": None,
                    "parts": done_ids,
                    "from_plan": True,
                    "date": key,
                }
            )
    weeks = []
    for week in view.get("weeks") or []:
        note = week.get("note") or {}
        weeks.append(
            {
                "start": week.get("start"),
                "end": week.get("end"),
                "title": note.get("title"),
                "note": note.get("text"),
                "targets": [
                    {
                        "title": t.get("title"),
                        "family": t.get("family"),
                        "time_s": t.get("time_s"),
                        "done_s": t.get("done_s"),
                    }
                    for t in week.get("targets") or []
                ],
                "done_s": per_week.get(str(week.get("start")), 0.0),
            }
        )
    # Planned per discipline and week, without optional sessions; the week
    # target per discipline when the plan has one.
    from vibesynergy.query import horizon

    planned_by_week: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"time_s": 0.0, "distance_m": 0.0})
    )
    for day in days.values():
        d = date.fromisoformat(day["date"])
        week_key = (d - timedelta(days=d.weekday())).isoformat()
        for p in day["planned"]:
            if horizon.optional(p.get("title")):
                continue
            fam = plan_q.family(p.get("sport")) or "other"
            planned_by_week[week_key][fam]["time_s"] += p.get("planned_s") or 0.0
            planned_by_week[week_key][fam]["distance_m"] += p.get("distance_m") or 0.0
    week_targets = {
        str(w["start"]): {t["family"]: t["time_s"] for t in w["targets"] if t.get("time_s")}
        for w in weeks
    }
    races = [
        {"date": str(r.get("date"))[:10], "title": r.get("title"), "priority": r.get("priority")}
        for r in view.get("races") or []
        if start.isoformat() <= str(r.get("date"))[:10] <= end.isoformat()
    ]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "today": today.isoformat(),
        "editable": editable,
        "days": list(days.values()),
        "weeks": weeks,
        "week_done": {k: round(v) for k, v in per_week.items()},
        "week_summary": {
            k: {
                **v,
                "time_s": round(v["time_s"]),
                "load": None if v["load"] is None else round(v["load"]),
                "sports": {
                    f: {"time_s": round(x["time_s"]), "distance_m": round(x["distance_m"])}
                    for f, x in v["sports"].items()
                },
            }
            for k, v in summary.items()
        },
        "week_planned": {
            k: {
                f: {
                    "time_s": round(x["time_s"]),
                    "distance_m": round(x["distance_m"]),
                    "target_s": week_targets.get(k, {}).get(f),
                }
                for f, x in fams.items()
            }
            for k, fams in planned_by_week.items()
        },
        "races": races,
    }
