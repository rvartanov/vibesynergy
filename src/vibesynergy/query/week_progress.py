"""One Monday-to-Sunday week against the plan, per sport (first part).

For each sport: the plan's weekly TARGET time (or, without one, the planned workouts), what
was done so far, and what is still planned. For each planned workout: planned against
actual, with intervals.icu's compliance when intervals.icu paired the activity with that
workout, and RPE/Feel. Activities that were not in the plan are listed as extra. Plus the
season and the week's note, and next week's note and targets.

The start page shows this week; the planning page can ask for any Monday. Matching starts
from ``plan.plan`` (same day and sport); an intervals.icu pairing overrides it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import activity_effort, horizon, icu_plan, training
from vibesynergy.query import plan as plan_q
from vibesynergy.store.schema import activity_unified

# Triathlon order first, then the rest.
SPORT_ORDER = ("swim", "ride", "run", "strength", "walk", None)
# A paired session within this compliance band (percent) went as planned.
COMPLIANCE_OK = (75.0, 125.0)


def compliance_of(planned_s, actual_s, icu_value=None) -> tuple[float | None, str | None]:
    """How much of the planned workout was done, in percent (every planned
    workout needs it). intervals.icu's own compliance when it paired the activity, else the
    time done against the time planned."""
    if icu_value:
        return round(float(icu_value), 1), "intervals"
    if planned_s and actual_s:
        return round(float(actual_s) / float(planned_s) * 100, 1), "time"
    return None, None


# Shorter activities without a sport (a transition, a quick test) are not listed as extra.
MIN_EXTRA_S = 300.0
_MULTI = ("multi", "triathlon", "duathlon", "brick")


def week_progress(
    engine: Engine,
    monday: date,
    today: date,
    *,
    plan: dict[str, Any] | None = None,
    raw=None,
    include_calendar: bool = True,
) -> dict[str, Any]:
    """The week starting ``monday`` against the plan. ``plan`` is a ``plan.plan`` result
    that covers this week (and, for the next-week line, the week after)."""
    monday = monday - timedelta(days=monday.weekday())
    sunday = monday + timedelta(days=6)
    if plan is None:
        plan = plan_q.plan(
            engine,
            Window(monday, sunday + timedelta(days=7)),
            today,
            include_calendar=include_calendar,
            include_sync_status=False,
        )
    lo, hi = monday.isoformat(), sunday.isoformat()
    planned = [
        item
        for item in plan.get("items") or []
        if item["kind"] in plan_q.TRAINING_KINDS and lo <= item["date"] <= hi
    ]
    activities = _activities(engine, monday, min(sunday, today))
    own = (
        training.own_loads(
            engine, Window(monday, min(sunday, today)), [a["group_id"] for a in activities]
        )
        if activities
        else {}
    )
    for act in activities:
        act["own_load"] = own.get(act["group_id"])
    icu = activity_effort.icu_for_groups(engine, raw, activities) if raw is not None else {}
    effort = activity_effort.for_groups(engine, raw, activities) if activities else {}
    by_id = {a["group_id"]: a for a in activities}
    paired = {row["paired_event_id"]: gid for gid, row in icu.items() if row["paired_event_id"]}

    used: set[str] = set()
    sessions: list[dict[str, Any]] = []
    for item in planned:
        session = _planned_session(item, today, by_id, paired, icu, effort)
        if session["activity_id"]:
            used.add(session["activity_id"])
        sessions.append(session)
    containers = _containers(activities)
    for act in activities:
        gid = act["group_id"]
        if gid in used or gid in containers:
            continue
        if act["family"] is None and _seconds(act) < MIN_EXTRA_S:
            continue
        sessions.append(_extra_session(act, icu.get(gid), effort.get(gid)))

    weeks = {w["start"]: w for w in plan.get("weeks") or []}
    this_week = weeks.get(lo)
    targets = {t["family"]: t for t in (this_week or {}).get("targets") or []}
    sports = _sports(sessions, activities, used, containers, targets, icu)
    counts = {
        status: sum(1 for s in sessions if s["status"] == status)
        for status in (
            "done",
            "changed",
            "missed",
            "optional",
            "uncertain",
            "today",
            "upcoming",
            "extra",
        )
    }
    nxt = weeks.get((monday + timedelta(days=7)).isoformat())
    season = icu_plan.season(engine, sunday)
    return {
        "start": lo,
        "end": hi,
        "today": str(today),
        "days_left": max(0, min(7, (sunday - today).days)) if today >= monday else 7,
        "season": season,
        "note": _note(this_week),
        "phase": plan.get("plan_phase"),
        "has_plan": bool(planned or targets),
        "sports": sports,
        "sessions": sorted(sessions, key=lambda s: (s["date"], s["family"] or "~")),
        "counts": counts,
        "totals": {
            "target_s": _sum(s["target_s"] for s in sports),
            "goal_s": _sum(s["goal_s"] for s in sports),
            "planned_s": _sum(s["planned_s"] for s in sports),
            "done_s": _sum(s["done_s"] for s in sports),
            "remaining_s": _sum(s["remaining_s"] for s in sports),
        },
        "next_week": _next_week(nxt),
    }


# Activities ----------------------------------------------------------------------------


def _activities(engine: Engine, start: date, end: date) -> list[dict[str, Any]]:
    if end < start:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                activity_unified.c.group_id,
                activity_unified.c.sources,
                activity_unified.c.start_utc,
                activity_unified.c.local_date,
                activity_unified.c.sport,
                activity_unified.c.name,
                activity_unified.c.moving_s,
                activity_unified.c.elapsed_s,
                activity_unified.c.distance_m,
                activity_unified.c.training_load,
                activity_unified.c.kind,
            )
            .where(activity_unified.c.local_date.between(start, end))
            .order_by(activity_unified.c.start_utc)
        ).mappings()
        out = [dict(row) for row in rows]
    for row in out:
        row["family"] = plan_q.family(row["sport"])
        row["sources"] = row.get("sources") or []
    return out


def _seconds(act: dict[str, Any]) -> float:
    return float(act.get("moving_s") or act.get("elapsed_s") or 0)


def _start(act: dict[str, Any]) -> datetime | None:
    value = act.get("start_utc")
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _containers(activities: list[dict[str, Any]]) -> set[str]:
    """Multisport activities whose parts are also there (the parts count, not the whole)."""
    out = set()
    for act in activities:
        if not any(word in (act.get("sport") or "").lower() for word in _MULTI):
            continue
        begin = _start(act)
        if begin is None:
            continue
        end = begin + timedelta(seconds=float(act.get("elapsed_s") or _seconds(act)))
        for other in activities:
            when = _start(other)
            if (
                other is not act
                and other["family"] is not None
                and when is not None
                and begin - timedelta(minutes=2) <= when <= end
            ):
                out.add(act["group_id"])
                break
    return out


# Sessions ------------------------------------------------------------------------------


def _effort(found: dict[str, Any] | None) -> dict[str, Any]:
    if not found:
        return {"rpe": None, "feel": None}
    return {
        "rpe": found["rpe"]["value"],
        "rpe_label": found["rpe"]["label"],
        "feel": found["feel"]["value"],
        "feel_label": found["feel"]["label"],
    }


def _actual(act: dict[str, Any], icu_row: dict[str, Any] | None) -> dict[str, Any]:
    del icu_row  # the load is VibeSynergy's own, not intervals.icu's
    return {
        "id": act["group_id"],
        "name": act.get("name"),
        "duration_s": round(_seconds(act), 1) or None,
        "distance_m": act.get("distance_m") or None,
        "load": act.get("own_load"),
        "coaching": act.get("kind") == "coaching",
    }


def _planned_session(item, today, by_id, paired, icu, effort) -> dict[str, Any]:
    extra = item.get("extra") or {}
    status = item.get("status") or "upcoming"
    match = (item.get("done_by") or [None])[0]
    gid = match.get("id") if match else None
    method = "day_sport" if gid else None
    icu_id = str(extra.get("icu_id") or "") or None
    if icu_id and icu_id in paired and paired[icu_id] in by_id:
        gid, method = paired[icu_id], "paired"
    act = by_id.get(gid) if gid else None
    icu_row = icu.get(gid) if gid else None
    compliance = None
    if method == "paired" and icu_row:
        compliance = icu_row.get("compliance")
        if compliance:
            low, high = COMPLIANCE_OK
            status = "done" if low <= compliance <= high else "changed"
        elif status not in ("done", "changed"):
            status = "changed" if plan_q.departs_from_plan(item, act or {}) else "done"
    # An optional session ("optie", "(optioneel)") is no debt and does not count towards
    # the week's goal. Not done, it is just "option": not "skipped", since rest
    # is often one of its choices ("rust, pilates of zwemmen").
    optional = horizon.optional(item.get("title"))
    if optional and status == "missed":
        status = "optional"
    compliance, compliance_source = compliance_of(
        plan_q.planned_duration_s(item), _seconds(act) if act else None, compliance
    )
    session = {
        "date": item["date"],
        "family": plan_q.family(item.get("sport")),
        "sport": item.get("sport"),
        "title": item.get("title"),
        "in_plan": True,
        "description": item.get("description"),
        "optional": optional,
        "status": status,
        "match": method,
        "compliance": compliance,
        "compliance_source": compliance_source,
        "planned": {
            "duration_s": plan_q.planned_duration_s(item),
            "distance_m": plan_q.planned_distance_m(item),
            "load": extra.get("load"),
            "intensity": plan_q.planned_intensity(item),
        },
        "actual": _actual(act, icu_row) if act else None,
        "activity_id": gid if act else None,
        "remaining": item["date"] > str(today) or status == "today",
    }
    session.update(_effort(effort.get(gid)) if act else {"rpe": None, "feel": None})
    return session


def _extra_session(act, icu_row, found) -> dict[str, Any]:
    return {
        "date": str(act["local_date"]),
        "family": act["family"],
        "sport": act.get("sport"),
        "title": act.get("name"),
        "in_plan": False,
        "status": "extra",
        "match": None,
        "compliance": None,
        "planned": None,
        "actual": _actual(act, icu_row),
        "activity_id": act["group_id"],
        "remaining": False,
        **_effort(found),
    }


# Per sport -----------------------------------------------------------------------------


def _sum(values) -> float:
    return round(sum(v or 0 for v in values), 1)


def _load(act: dict[str, Any], icu: dict[str, dict]) -> float | None:
    """VibeSynergy's own load of the activity (``training.own_loads``)."""
    del icu
    return act.get("own_load")


def _sports(sessions, activities, used, containers, targets, icu) -> list[dict[str, Any]]:
    families = {s["family"] for s in sessions} | set(targets)
    out = []
    for fam in sorted(families, key=lambda f: SPORT_ORDER.index(f) if f in SPORT_ORDER else 9):
        mine = [s for s in sessions if s["family"] == fam]
        planned = [s for s in mine if s["in_plan"]]
        # Done: everything of this sport, except a training you gave that matched no plan
        # workout, and a multisport whole whose parts are counted.
        done = [
            a
            for a in activities
            if a["family"] == fam
            and a["group_id"] not in containers
            and (a["kind"] != "coaching" or a["group_id"] in used)
            and (a["family"] is not None or _seconds(a) >= MIN_EXTRA_S)
        ]
        target = targets.get(fam) or {}
        remaining = [s for s in planned if s["remaining"] and not s.get("optional")]
        target_s = target.get("time_s")
        # The goal leaves optional sessions out: the target counts them in, the athlete may
        # skip them, so skipping must not show as a shortfall (8-11: "fietsuren tekort").
        optional_s = _sum(
            (s["planned"] or {}).get("duration_s") for s in planned if s.get("optional")
        )
        required = [s for s in planned if not s.get("optional")]
        planned_s = _sum((s["planned"] or {}).get("duration_s") for s in required)
        goal_s = max(0.0, target_s - optional_s) if target_s else planned_s
        out.append(
            {
                "family": fam,
                "sport": target.get("sport")
                or next((s["sport"] for s in planned), None)
                or plan_q._SPORT.get(fam or "")
                or (mine[0]["sport"] if mine else None),
                "target_title": target.get("title"),
                "target_s": target_s,
                "target_distance_m": target.get("distance_m"),
                "target_load": target.get("load"),
                "planned_s": planned_s or None,
                "planned_count": len(required),
                "optional_s": optional_s or None,
                "planned_load": _sum((s["planned"] or {}).get("load") for s in planned) or None,
                "goal_s": goal_s or None,
                "basis": "target" if target_s else "planned" if planned_s else None,
                "done_s": _sum(_seconds(a) for a in done),
                "done_distance_m": _sum(a.get("distance_m") for a in done) or None,
                "done_load": _sum(_load(a, icu) for a in done) or None,
                "done_count": len(done),
                "remaining_s": _sum((s["planned"] or {}).get("duration_s") for s in remaining),
                "remaining": [
                    {
                        "date": s["date"],
                        "title": s["title"],
                        "duration_s": (s["planned"] or {}).get("duration_s"),
                        "status": s["status"],
                    }
                    for s in remaining
                ],
                "counts": {
                    status: sum(1 for s in mine if s["status"] == status)
                    for status in ("done", "changed", "missed", "optional", "uncertain", "extra")
                },
            }
        )
    return out


# Context -------------------------------------------------------------------------------


def _note(week: dict[str, Any] | None) -> dict[str, Any] | None:
    note = (week or {}).get("note")
    if not note:
        return None
    text = (note.get("text") or "").strip()
    return {
        "title": note.get("title"),
        "focus": text.split("\n\n", 1)[0].strip() if text else None,
        "text": text or None,
    }


def _next_week(week: dict[str, Any] | None) -> dict[str, Any] | None:
    if not week:
        return None
    return {
        "start": week["start"],
        "title": ((week.get("note") or {}).get("title")),
        "time_s": week.get("time_s"),
        "targets": [
            {"sport": t["sport"], "family": t["family"], "time_s": t["time_s"]}
            for t in week.get("targets") or []
            if t.get("time_s")
        ],
    }
