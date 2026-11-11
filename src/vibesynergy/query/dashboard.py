"""The Dashboard: how you are and what to do today, from fixed, explainable rules.

- Verdict: recovery (readiness, HRV and resting HR against your own baseline, last night's
  sleep, form) combined with today's planned session, the coaching plan and the next race.
- Needs attention: recovery, training load, injuries and the system, only when something
  is off; each item says which rule fired.
- Plan, this week and trends: the coaching plan and race countdown, this week against your
  usual week, and the notable changes of the last four weeks.

No AI: the same inputs always give the same words. It is guidance, not medical advice.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.i18n import ENGLISH, Translator
from vibesynergy.query import (
    analysis,
    journal,
    latency,
    safety_floor,
    training,
    views,
    week_progress,
)
from vibesynergy.query import plan as plan_q
from vibesynergy.query import series as qs
from vibesynergy.store.schema import (
    activity_unified,
    checkin,
    daily_metric,
    job,
    planned_item,
)

log = logging.getLogger(__name__)
BLOCK_ERROR = "unavailable"

BASELINE_DAYS = 60
# The floor owns the numbers. These names stay so attention and older imports keep working.
HRV_LOW_Z = safety_floor.THRESHOLDS["hrv_z"]
HRV_LOW_STREAK = safety_floor.THRESHOLDS["hrv_low_streak"]
RHR_UP_BPM = safety_floor.THRESHOLDS["rhr_up_bpm"]
SHORT_SLEEP_S = safety_floor.THRESHOLDS["short_sleep_s"]
IRREGULAR_SLEEP_SD_MIN = 60
ACWR_HIGH = safety_floor.THRESHOLDS["acwr_high"]
TSB_DEEP = safety_floor.THRESHOLDS["tsb_deep"]
IDLE_DAYS = 7
STALE_HOURS = 24

HARD_WORDS = safety_floor.HARD_WORDS
EASY_WORDS = safety_floor.EASY_WORDS
TREND_METRICS = (
    ("hr.resting", "Resting heart rate", "bpm", 2.0, False),
    ("hrv.night_avg", "Overnight HRV", "ms", 3.0, True),
    ("sleep.duration", "Time asleep", "s", 20 * 60, True),
    ("body.weight", "Weight", "kg", 0.8, None),
)


def _block(fn):
    """One section. A failure stays in that section; the rest of the page still returns."""
    try:
        return fn()
    except Exception:
        log.exception("dashboard block failed")
        return {"error": BLOCK_ERROR}


def _failed(value: Any) -> bool:
    return isinstance(value, dict) and value.get("error") == BLOCK_ERROR and len(value) == 1


def _load_runtime(rt, engine: Engine, athlete: dict | None):
    if rt is not None:
        return rt
    return SimpleNamespace(engine=engine, settings=SimpleNamespace(athlete=athlete))


def _has_athlete_data(engine: Engine) -> bool:
    probes = (
        (activity_unified, activity_unified.c.group_id),
        (daily_metric, daily_metric.c.metric),
        (planned_item, planned_item.c.source_id),
        (checkin, checkin.c.date),
    )
    with engine.connect() as conn:
        for _table, column in probes:
            if conn.scalar(select(column).limit(1)) is not None:
                return True
    return False


def dashboard(
    engine: Engine,
    today: date,
    *,
    timezone: str,
    athlete: dict | None,
    sources: list[str],
    gear: list[dict] | None = None,
    tr: Translator = ENGLISH,
    include_calendar: bool = True,
    rt=None,
) -> dict[str, Any]:
    if not sources and not _has_athlete_data(engine):
        return {"onboarding": True, "date": str(today), "updated_at": utc_now().isoformat()}

    load_rt = _load_runtime(rt, engine, athlete)
    completed = _block(lambda: _completed(engine, today))
    recovery = _block(lambda: _recovery(engine, today, timezone))
    load = _block(lambda: _load(engine, today, athlete, load_rt))
    plan = _block(
        lambda: plan_q.plan(
            engine,
            Window(today - timedelta(days=today.weekday()), today + timedelta(days=21)),
            today,
            include_calendar=include_calendar,
        )
    )
    injuries = _block(lambda: journal.injuries(engine, today, days=14, tr=tr))
    sick = _block(
        lambda: journal.tagged_days(engine, Window(today - timedelta(days=2), today), ["sick"])
    )
    checkin_tags = _block(lambda: _checkin_and_tags(engine, today))
    attention = _block(
        lambda: (
            _attention_block(
                engine, today, recovery, load, injuries, sources, tr, gear, _horizon_args(rt)
            )
            + _coach_runs(engine, timezone, tr)
        )
    )
    queried_at = utc_now().isoformat()
    delivery = _block(lambda: delivery_status(engine, sources, queried_at=queried_at))
    verdict = _block(
        lambda: _verdict_block(recovery, load, plan, injuries, sick, checkin_tags, tr, completed)
    )
    plan_view = _block(lambda: _plan_view(plan, today))
    week = _block(lambda: _week(engine, today, None if _failed(plan) else plan))
    progress = _block(
        lambda: _week_progress(engine, today, plan, getattr(rt, "raw", None), include_calendar)
    )
    trends = _block(lambda: _trends(engine, today, tr))
    open_injuries = _block(lambda: _open_injuries(injuries))
    result = {
        "date": str(today),
        "onboarding": False,
        "next_race": None if _failed(plan) else plan["next_race"],
        "plan_phase": None if _failed(plan) else plan["plan_phase"],
        "supporting": [] if _failed(plan) else (plan.get("supporting") or []),
        "updated_at": queried_at,
        "delivery": delivery,
        "completed": completed,
        "verdict": verdict,
        "attention": attention,
        "recovery": recovery,
        "load": load,
        "plan": plan_view,
        "week": week,
        "week_progress": progress,
        "trends": trends,
        "injuries": open_injuries,
    }
    if _failed(verdict):
        result["safety"] = {"error": BLOCK_ERROR}
    else:
        result["safety"] = {
            "level": verdict["safety_level"],
            "rules": verdict["rules"],
        }
    if not _failed(completed):
        latency.dashboard_visible(engine, completed["activities"])
    return result


def _completed(engine: Engine, today: date) -> dict[str, Any]:
    completed = views.activities(engine, Window(today, today), limit=50)
    with engine.connect() as conn:
        changed = conn.scalar(
            select(func.max(activity_unified.c.updated_at)).where(
                activity_unified.c.local_date == today
            )
        )
    return {**completed, "changed_at": changed.isoformat() if changed else None}


def _horizon_args(rt) -> tuple[int, tuple[str, ...]]:
    from vibesynergy.query import horizon

    data_dir = getattr(getattr(rt, "settings", None), "data_dir", None)
    if data_dir is None:
        return horizon.DAYS, ()
    from vibesynergy.portal import prefs

    stored = prefs.load(data_dir)
    return prefs.horizon_days(data_dir), tuple(stored.get("garmin_delivery") or ())


def _coach_runs(engine, timezone: str, tr) -> list:
    """A coach routine that did not run: the athlete hears it, the plan may be stale."""
    from vibesynergy.query import coach_tasks

    try:
        tasks = coach_tasks.status(engine, utc_now(), timezone)
    except Exception:  # the start page must still answer
        return []
    return [
        {
            "level": "warn",
            "area": "coach",
            "message": tr(
                "Your coach skipped the {task} ({day}).", task=tr(task["title"]).lower(), day=day
            ),
            "link": "#/coach",
        }
        for task in tasks
        for day in task["missing"]
    ]


def _attention_block(
    engine, today, recovery, load, injuries, sources, tr, gear, horizon_args=(6, ())
) -> list:
    if any(_failed(part) for part in (recovery, load, injuries)):
        raise RuntimeError("attention inputs failed")
    attention = _attention(engine, today, recovery, load, injuries, sources, tr)
    from vibesynergy.query import horizon

    guard = horizon.status(engine, today, *horizon_args)
    for item in guard["missing"]:
        attention.append(
            {
                "level": "warning",
                "area": "plan",
                "message": tr(
                    "Not on your Garmin yet: {title} ({day}).",
                    title=item["title"],
                    day=item["date"],
                ),
                "link": "#/planning",
            }
        )
    for item in guard["duplicates"]:
        attention.append(
            {
                "level": "info",
                "area": "plan",
                "message": tr(
                    "Twice on your Garmin: {title} ({day}).", title=item["title"], day=item["date"]
                ),
                "link": "#/planning",
            }
        )
    for item in guard.get("outdated") or []:
        attention.append(
            {
                "level": "warning",
                "area": "plan",
                "message": tr(
                    "Old version on your Garmin: {title} ({day}).",
                    title=item["title"],
                    day=item["date"],
                ),
                "link": "#/planning",
            }
        )
    fill = guard.get("fill") or {}
    if fill.get("behind"):
        through = fill.get("planned_through")
        short = (
            through is None
            or (date.fromisoformat(guard["through"]) - date.fromisoformat(through)).days >= 3
        )
        attention.append(
            {
                # A short plan for days on end means the coach did not fill it: tell the athlete.
                "level": "warn" if short else "warning",
                "area": "plan",
                "message": tr(
                    "Your plan runs only to {day}. Ask your coach to fill it up to {end}.",
                    day=through or guard["from"],
                    end=guard["through"],
                ),
                "link": "#/planning",
            }
        )
    for g in gear or []:  # maintenance that is due
        for svc in g.get("services") or []:
            if svc.get("state") == "due":
                attention.append(
                    {
                        "level": "info",
                        "area": "gear",
                        "message": tr(
                            "{name}: {what} is due.", name=g.get("name") or "", what=svc["what"]
                        ),
                        "link": "#/gear",
                    }
                )
    for g in gear or []:  # worn shoes you still run in
        if g.get("warning") and g.get("used_last_30_days"):
            past = g.get("warning_level") == "past"
            attention.append(
                {
                    "level": "info",
                    "area": "gear",
                    "message": tr("{name}: past its lifetime, replace it soon.", name=g["name"])
                    if past
                    else tr(
                        "{name}: close to its lifetime, time to look for a new one.",
                        name=g["name"],
                    ),
                    "link": "#/gear",
                }
            )
    return attention


def _verdict_block(recovery, load, plan, injuries, sick, checkin_tags, tr, completed) -> dict:
    if any(
        _failed(part) for part in (recovery, load, plan, injuries, sick, checkin_tags, completed)
    ):
        raise RuntimeError("verdict inputs failed")
    checkin, tags = checkin_tags
    return _verdict(
        recovery,
        load,
        plan,
        injuries,
        bool(sick),
        tr,
        completed_count=completed["totals"]["count"],
        checkin=checkin,
        tags=tags,
    )


def _plan_view(plan: dict, today: date) -> dict:
    if _failed(plan):
        raise RuntimeError("plan failed")
    return {
        "active": plan["active_plans"],
        "today": plan["today"],
        "next_event": plan["next_event"],
        "races": plan["races"],
        "week": [
            i
            for i in plan["items"]
            if i["kind"] != "event"
            and i["date"] <= str(today + timedelta(days=6 - today.weekday()))
        ],
        "adherence": plan["adherence"],
        "plan_source": plan.get("plan_source"),
        "not_in_plan": [
            i
            for i in plan.get("not_in_plan") or []
            if i["date"] <= str(today + timedelta(days=6 - today.weekday()))
        ],
        "weeks": plan.get("weeks") or [],
    }


def _week_progress(engine, today: date, plan, raw, include_calendar: bool) -> dict:
    if _failed(plan):
        raise RuntimeError("plan failed")
    return week_progress.week_progress(
        engine,
        today - timedelta(days=today.weekday()),
        today,
        plan=plan,
        raw=raw,
        include_calendar=include_calendar,
    )


def _open_injuries(injuries) -> list:
    if _failed(injuries):
        raise RuntimeError("injuries failed")
    return [
        {k: i[k] for k in ("id", "title", "status", "latest_pain", "flags", "pain_log")}
        for i in injuries
        if i["status"] != "resolved"
    ]


def delivery_status(engine: Engine, sources: list[str], *, queried_at: str) -> dict[str, Any]:
    """Arrival status for one dashboard read. Does not start sync or assessment work.

    ``last_collected_at`` is the newest successful collection of any dataset for that
    source. It does not mean every dataset, or today's workout, is current.
    ``activity_sync`` watches the activities dataset only: a queued or running sync of
    that source (or the whole source), or current activity rows not yet normalised, is
    ``running``. A fetch or mapping problem with no active sync is ``failed``. Otherwise
    ``idle``. A history window is a backfill, not today's arrival.
    ``follow_up`` stays ``idle``. Internal assessment and daily-review jobs are gone.
    """
    status = views.sync_status(engine)
    fresh = views.freshness(engine)
    enabled = set(sources)
    visible: dict[str, str | None] = {
        source: at
        for source, at in fresh.items()
        if not source.endswith("_export") and source != "files"
    }
    for source in sources:
        if source.endswith("_export") or source == "files":
            continue
        visible.setdefault(source, None)
    activity = [
        row
        for row in status["datasets"]
        if row["source"] in enabled and row["dataset"] == "activities"
    ]
    problems = [row for row in activity if row["problem_stages"]]
    pending = any(row["processing"]["state"] == "pending" for row in activity)
    if _activity_sync_jobs(engine, enabled) or (pending and not problems):
        activity_sync = "running"
    elif problems:
        activity_sync = "failed"
    else:
        activity_sync = "idle"
    return {
        "queried_at": queried_at,
        "sources": [
            {"source": source, "last_collected_at": visible[source]} for source in sorted(visible)
        ],
        "activity_sync": activity_sync,
        "follow_up": "running" if _follow_up_jobs(engine) else "idle",
    }


def _activity_sync_jobs(engine: Engine, sources: set[str]) -> bool:
    with engine.connect() as conn:
        rows = conn.execute(
            select(job.c.payload).where(
                job.c.kind == "sync", job.c.status.in_(("queued", "running"))
            )
        ).all()
    for (payload,) in rows:
        if not isinstance(payload, dict) or payload.get("source") not in sources:
            continue
        if payload.get("window"):
            continue
        if payload.get("dataset") in (None, "", "activities"):
            return True
    return False


def _follow_up_jobs(engine: Engine) -> bool:
    """Internal assessment jobs are gone. Follow-up is idle."""
    del engine
    return False


# Recovery -----------------------------------------------------------------------------


def _recovery(engine: Engine, today: date, timezone: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric in (
        "recovery.readiness",
        "hrv.night_avg",
        "hr.resting",
        "sleep.score",
        "sleep.duration",
    ):
        b = qs.baseline(engine, metric, today, window_days=BASELINE_DAYS)
        out[metric] = b
    # HRV below the usual range for several days running: each day against its own 60 days.
    hrv = qs.daily_values(
        engine, ["hrv.night_avg"], Window(today - timedelta(days=BASELINE_DAYS + 7), today)
    )["hrv.night_avg"]
    streak = 0
    for back in range(7):
        day = today - timedelta(days=back)
        history = [v for d, v in hrv.items() if day - timedelta(days=BASELINE_DAYS) <= d < day]
        if day not in hrv or len(history) < 7:
            break
        mean, sd = statistics.fmean(history), statistics.stdev(history)
        if not sd or (hrv[day] - mean) / sd >= HRV_LOW_Z:
            break
        streak += 1
    out["hrv_low_days"] = streak
    nights = views.sleep(engine, Window(today - timedelta(days=13), today), timezone=timezone)
    out["sleep_regularity"] = nights.get("regularity")
    return out


def _z(b: dict | None) -> float | None:
    return b["z"] if b else None


def _reading_value(block) -> float | None:
    """Today's number, or None when the reading is missing.

    A block with a null value is not a reading. Resting heart rate is a separate
    signal and is not part of this picture.
    """
    if not isinstance(block, dict):
        return None
    value = block.get("value")
    if value is None:
        return None
    return value


# Load ---------------------------------------------------------------------------------


def _load(engine: Engine, today: date, athlete: dict | None, rt) -> dict[str, Any]:
    # Form (CTL/ATL/TSB) is the shared 180-day snapshot. ACWR uses the same intervals.icu
    # loads over the last 28 days, so every load number comes from one model.
    current = training.current_load(rt, today)
    pmc = training.training_load(
        engine,
        Window(today - timedelta(days=27), today),
        athlete=athlete,
        load="intervals",
        raw=getattr(rt, "raw", None),
    )
    loads = [row[1] for row in pmc["rows"]]
    acute = statistics.fmean(loads[-7:]) if loads else 0.0
    chronic = statistics.fmean(loads) if loads else 0.0
    with engine.connect() as conn:
        last = conn.execute(
            select(func.max(activity_unified.c.local_date)).where(
                activity_unified.c.local_date <= today
            )
        ).scalar()
    merged = {**(pmc["current"] or {}), **current}
    return {
        **merged,
        "acwr": round(acute / chronic, 2) if chronic else None,
        "days_since_training": (today - last).days if last else None,
    }


# Verdict ------------------------------------------------------------------------------


def _intensity(session: dict) -> str:
    if session.get("coached"):
        return "easy"  # a training you give: you run along easy
    return safety_floor.title_band(session.get("title"))


def _checkin_and_tags(engine: Engine, today: date) -> tuple[dict | None, list[str]]:
    """Today's check-in, plus note tags from yesterday and today (same window as coaching)."""
    tags: set[str] = set()
    checkin = None
    for offset in (1, 0):
        day = journal.day_journal(engine, today - timedelta(days=offset))
        for note in day["notes"]:
            tags.update(note.get("tags") or [])
        if offset == 0 and day.get("checkin"):
            checkin = day["checkin"]
    return checkin, sorted(tags)


def _floor_reason(item: dict, tr: Translator) -> str:
    values = dict(item.get("values") or {})
    if "seconds" in item:
        values["time"] = _hm(item["seconds"], tr)
    return tr(item["key"], **values)


def _verdict(
    recovery,
    load,
    plan,
    injuries,
    sick: bool,
    tr: Translator = ENGLISH,
    *,
    completed_count: int = 0,
    checkin: dict | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Colour and sentences for the dashboard. The level comes from the shared floor."""
    floor = safety_floor.evaluate(
        safety_floor.signals_from_dashboard(
            recovery, load, injuries, sick, checkin=checkin, tags=tags
        )
    )
    color = {"ok": "green", "ease": "amber", "flag": "amber", "refuse": "red"}[floor["level"]]
    hrv = recovery["hrv.night_avg"]
    sleep, readiness = recovery["sleep.duration"], recovery["recovery.readiness"]
    reasons = [_floor_reason(item, tr) for item in floor["reasons"]]
    # HRV, sleep and readiness are the recovery picture. Missing values, including a
    # block whose value is null, are not a green light. Resting HR on its own is not
    # either. It is not a floor rule: coaching has the same empty inputs and stays ok,
    # so the shared level does not move.
    hrv_value, sleep_value = _reading_value(hrv), _reading_value(sleep)
    readiness_value = _reading_value(readiness)
    picture_missing = hrv_value is None and sleep_value is None and readiness_value is None
    if floor["level"] == "ok" and picture_missing:
        color = "amber"
        reasons = [tr("not enough recovery data for today's advice")]
    good: list[str] = []
    if floor["level"] == "ok" and color == "green":
        z = _z(hrv)
        if hrv_value is not None and z is not None and z >= 0:
            good.append(tr("HRV normal or better"))
        if sleep_value is not None and sleep_value >= 7 * 3600:
            good.append(tr("slept {time}", time=_hm(sleep_value, tr)))
    shown = (reasons or good)[:3]
    headline = {
        "green": tr("Well recovered"),
        "amber": tr("Take it a bit easier"),
        "red": tr("Rest or keep it very easy"),
    }[color]
    advice = _advice(color, plan, tr, completed_count=completed_count)
    return {
        "level": color,
        "safety_level": floor["level"],
        "rules": floor["rules"],
        "headline": headline,
        "reasons": shown,
        "signals": [
            {"rule": item["rule"], "text": _floor_reason(item, tr)}
            for item in floor["reasons"]
            if item.get("rule")
        ],
        "advice": advice,
        "text": f"{headline}: {', '.join(shown)}. {advice}" if shown else f"{headline}. {advice}",
    }


def _advice(
    level: str,
    plan: dict[str, Any],
    tr: Translator = ENGLISH,
    *,
    completed_count: int = 0,
) -> str:
    sessions = plan["today"]
    race = plan["next_race"]
    taper = plan["plan_phase"] and plan["plan_phase"]["phase"] == "taper"
    finished = [s for s in sessions if s["status"] in ("done", "changed")]
    todo = [s for s in sessions if s["status"] not in ("done", "changed")]
    if any(s["status"] == "uncertain" for s in sessions) and level != "red":
        tail = tr(
            "The plan cannot be matched reliably to completed activity. "
            "Review it before adding training."
        )
    elif finished and not todo:
        changed = next((s for s in finished if s["status"] == "changed"), None)
        if changed:
            tail = tr(
                "Today's {title} differed from the plan. Recover before you add more.",
                title=changed["title"],
            )
        else:
            tail = tr("Today's {title} is done; recover well.", title=finished[0]["title"])
    elif completed_count and level != "red":
        tail = (
            tr("One activity recorded today.")
            if completed_count == 1
            else tr("{n} activities recorded today.", n=completed_count)
        )
        tail += " " + tr("Review what you have already done before adding training.")
        if todo:
            tail += " " + tr("Still on the plan: {title}.", title=todo[0]["title"])
    elif todo:
        s = todo[0]
        kind = _intensity(s)
        if level == "green":
            tail = tr("Today's plan: {title}, go ahead.", title=s["title"])
        elif level == "amber":
            tail = (
                tr(
                    "Today's plan: {title}; do it controlled or swap for an easy session.",
                    title=s["title"],
                )
                if kind == "hard"
                else tr("Today's plan: {title}, fine at an easy effort.", title=s["title"])
            )
        else:
            tail = tr("Swap today's {title} for rest or a very easy session.", title=s["title"])
    else:
        tail = {
            "green": tr("Nothing planned: a good day for quality if you feel like it."),
            "amber": tr("Nothing planned: keep any training easy."),
            "red": tr("Nothing planned: rest."),
        }[level]
    if taper:
        tail += " " + tr(
            "{race} in {n} days: protect freshness, nothing new.",
            race=race["name"],
            n=race["days_to_go"],
        )
        if any(_intensity(s) == "hard" for s in todo):
            tail += " " + tr("Warning: this hard session may conflict with the taper.")
    return tail


# Needs attention -----------------------------------------------------------------------


def _attention(
    engine, today, recovery, load, injuries, sources, tr: Translator = ENGLISH
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def add(level: str, area: str, message: str, link: str) -> None:
        items.append({"level": level, "area": area, "message": message, "link": link})

    if recovery["hrv_low_days"] >= HRV_LOW_STREAK:
        add(
            "warn",
            "recovery",
            tr("HRV below your usual range {n} days running.", n=recovery["hrv_low_days"]),
            "#/metrics?m=hrv.night_avg",
        )
    rhr = recovery["hr.resting"]
    if rhr and rhr["value"] - rhr["baseline_mean"] >= RHR_UP_BPM:
        add(
            "warn",
            "recovery",
            tr(
                "Resting heart rate is {up} bpm above your usual, often an early sign of "
                "illness or strain. Take it easy.",
                up=round(rhr["value"] - rhr["baseline_mean"]),
            ),
            "#/metrics?m=hr.resting",
        )
    reg = recovery.get("sleep_regularity")
    if reg and reg["bedtime"]["sd_min"] > IRREGULAR_SLEEP_SD_MIN:
        add(
            "info",
            "sleep",
            tr(
                "Your bedtime has varied by about {n} min lately; a steadier one helps recovery.",
                n=reg["bedtime"]["sd_min"],
            ),
            "#/sleep",
        )
    if (load.get("acwr") or 0) > ACWR_HIGH:
        add(
            "warn",
            "training",
            tr(
                "Training has ramped up fast: this week is {x} times your 4-week "
                "average. Ease off to stay injury-free.",
                x=load["acwr"],
            ),
            "#/training",
        )
    if load.get("form") == "overreaching risk":
        add(
            "warn",
            "training",
            tr("You're carrying a lot of fatigue: take it easy for a few days."),
            "#/training",
        )
    if (load.get("days_since_training") or 0) >= IDLE_DAYS:
        add(
            "info",
            "training",
            tr("No training for {n} days.", n=load["days_since_training"]),
            "#/activities",
        )
    for i in injuries:
        for f in i["flags"]:
            add(
                "stop" if f["level"] == "stop" else "warn",
                "injury",
                f"{i['title']}: {f['message']}",
                "#/dashboard?section=diary",
            )
    status = views.sync_status(engine)
    for p in status["problems"]:
        if p["source"] in sources and any(
            w in (p["last_error"] or "") for w in ("AuthError", "not logged in", "invalid_grant")
        ):
            add(
                "warn",
                "system",
                tr(
                    "{source}: login needed ({dataset}).",
                    source=p["source"].title(),
                    dataset=p["dataset"],
                ),
                "#/settings",
            )
    now = utc_now()
    for source, at in views.freshness(engine).items():
        if source in sources and (now - _parse(at)).total_seconds() > STALE_HOURS * 3600:
            add(
                "info",
                "system",
                tr(
                    "{source} hasn't synced for {n} hours.",
                    source=source.title(),
                    n=round((now - _parse(at)).total_seconds() / 3600),
                ),
                "#/settings/data/sync",
            )
    order = {"stop": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["level"]])


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# This week and trends ------------------------------------------------------------------


_CHIP_RANK = {"missed": 0, "changed": 1, "uncertain": 2, "today": 3, "done": 4, "upcoming": 5}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _planned_piece(item: dict) -> dict[str, float]:
    """Distance in metres, duration in seconds, climb in metres, from a calendar extra."""
    extra = item.get("extra") or {}
    duration = _number(extra.get("duration_s")) or _number(extra.get("duration"))
    distance = _number(extra.get("distance_m")) or _number(extra.get("distance"))
    ascent = _number(
        extra.get("ascent_m")
        or extra.get("ascent")
        or extra.get("elevation")
        or extra.get("elevationGain")
    )
    target = extra.get("completionTarget")
    if isinstance(target, dict):
        unit = str(target.get("unitType") or "")
        value = _number(target.get("value"))
        if value is not None:
            # Garmin completion targets use kilometres for distance. See race_phase.
            if unit == "distance" and distance is None:
                distance = value * 1000
            elif unit in {"time", "duration"} and duration is None:
                duration = value
            elif unit in {"elevation", "ascent"} and ascent is None:
                ascent = value
    return {
        "moving_s": duration or 0.0,
        "distance_m": distance or 0.0,
        "ascent_m": ascent or 0.0,
    }


def _week_chips(items: list[dict], today: date, monday: date) -> list[dict[str, str]]:
    by_day: dict[str, list[dict]] = {}
    for item in items:
        by_day.setdefault(item["date"], []).append(item)
    chips = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        key = day.isoformat()
        rows = by_day.get(key) or []
        if not rows:
            continue
        chosen = min(rows, key=lambda row: _CHIP_RANK.get(row.get("status"), 9))
        chips.append(
            {
                "date": key,
                "status": chosen.get("status") or "upcoming",
                "title": chosen.get("title") or "",
            }
        )
    return chips


def _week(engine: Engine, today: date, plan: dict | None) -> dict[str, Any]:
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    this = training.volume(engine, Window(monday, today), granularity="week", by="all")[
        "totals"
    ].get("all", {})
    past = training.volume(
        engine,
        Window(monday - timedelta(weeks=8), monday - timedelta(days=1)),
        granularity="week",
        by="all",
    )
    # Averaged over all 8 weeks, including weeks without training.
    usual = {
        k: round(sum(b.get(k, 0) for b in past["buckets"]) / 8, 1)
        for k in ("moving_s", "distance_m", "ascent_m", "count")
    }
    items = []
    if plan:
        items = [
            i
            for i in plan["items"]
            if i["kind"] in plan_q.TRAINING_KINDS
            and monday.isoformat() <= i["date"] <= sunday.isoformat()
        ]
    week_plan = next(
        (w for w in (plan or {}).get("weeks") or [] if w["start"] == monday.isoformat()), None
    )
    targets = [t for t in (week_plan or {}).get("targets") or [] if t.get("time_s")]
    basis = None
    if items:
        target = {"moving_s": 0.0, "distance_m": 0.0, "ascent_m": 0.0, "count": float(len(items))}
        for item in items:
            piece = _planned_piece(item)
            for key in ("moving_s", "distance_m", "ascent_m"):
                target[key] += piece[key]
        compare = "plan"
        basis = "sessions"
    else:
        target = usual
        compare = "usual"
    if targets:
        # The weekly TARGET time per sport is the plan's own number for the week.
        if compare != "plan":
            target = {"moving_s": 0.0, "distance_m": 0.0, "ascent_m": 0.0, "count": 0.0}
        target = {**target, "moving_s": float(sum(t["time_s"] for t in targets))}
        compare = "plan"
        basis = "targets"
    actual = {
        "moving_s": this.get("moving_s") or 0,
        "distance_m": this.get("distance_m") or 0,
        "ascent_m": this.get("ascent_m") or 0,
        "count": this.get("count") or 0,
    }
    return {
        "start": str(monday),
        "days": today.weekday() + 1,
        "compare": compare,
        "this": this,
        "target": target,
        "usual_week": usual,
        "actual": actual,
        "chips": _week_chips(items, today, monday) if items else [],
        "basis": basis,
        "targets": [
            {
                "sport": t["sport"],
                "title": t["title"],
                "planned_s": t["time_s"],
                "done_s": t["done_s"],
            }
            for t in targets
        ],
        "note": (week_plan or {}).get("note"),
    }


def _trends(engine: Engine, today: date, tr: Translator = ENGLISH) -> list[dict[str, Any]]:
    recent = Window(today - timedelta(days=27), today)
    before = analysis.previous_window(recent, "previous")
    result = analysis.compare(
        engine, [m for m, *_ in TREND_METRICS], recent, before, exclude_tags=["sick"]
    )
    out = []
    for metric, label, unit, minimum, higher_better in TREND_METRICS:
        r = result.get(metric, {})
        diff = r.get("difference")
        if diff is None or abs(diff) < minimum or (r.get("mann_whitney_p") or 1) > 0.05:
            continue
        good = None if higher_better is None else (diff > 0) == higher_better
        amount = _hm(abs(diff), tr) if unit == "s" else f"{abs(diff):.1f} {unit}"
        name = tr(label)
        text = (
            tr("{metric} up {amount} over the last 4 weeks.", metric=name, amount=amount)
            if diff > 0
            else tr("{metric} down {amount} over the last 4 weeks.", metric=name, amount=amount)
        )
        out.append(
            {
                "metric": metric,
                "text": text,
                "good": good,
            }
        )
    return out


def _hm(seconds: float, tr: Translator = ENGLISH) -> str:
    minutes = round(seconds / 60)
    if minutes >= 60:
        return tr("{h} h {m} min", h=minutes // 60, m=f"{minutes % 60:02d}")
    return tr("{m} min", m=minutes)
