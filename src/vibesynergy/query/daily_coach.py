"""Morning decision and evening review: stored, versioned views of the shared coaching state.

Coach entries and the safety floor. The inputs reuse the activity
assessments, the personal calendar, recovery, the diary and the active coach profile; the
fixed safety rules below decide, before any model call, when advice must be softened (flag)
or when no training advice is given at all (refuse). Reading never creates work.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import desc, select

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.i18n import translator
from vibesynergy.query import (
    assessments,
    coach,
    evidence,
    journal,
    plan,
    rhythm,
    series,
    snapshot,
    training,
    views,
)
from vibesynergy.store.schema import activity_unified, coach_decision_log, daily_unified

SCHEMA_VERSION = 1
PROMPT_VERSION = 2
# Keep bounded invalidation: wording changes should not force every stored review stale.
INPUT_PROMPT_VERSION = 1
KINDS = ("morning", "evening")
RHYTHM_KINDS = ("weekly", "block")
ALL_KINDS = (*KINDS, *RHYTHM_KINDS)
SUBJECT = 1
CONTEXT_DAYS = 14
RECOVERY = assessments.METRICS

# Rule names stored on review rows. ``safety_floor.evaluate`` decides the level and may
# add dashboard rules (hrv_rhr, deep_fatigue, acwr_high, tsb_deep, rhr_up) beside these.
# Sick is refuse (rest), matching the dashboard's red day.
SAFETY_RULES = {
    "affects_movement": "refuse",
    "pain_7": "refuse",
    "red_flag_note": "refuse",
    "injury_warning": "flag",
    "sick": "refuse",
    "short_sleep": "flag",
    "low_readiness": "flag",
    "low_hrv": "flag",
    "sore": "flag",
    "load_spike": "flag",
}
_SAFETY_LEVELS = ("ok", "ease", "flag", "refuse")


def safety(inputs: dict) -> dict:
    """Fixed rules for these inputs. The shared floor decides the level.

    Stored rows keep the rule names. A missing soreness score is not zero.
    """
    from vibesynergy.query.safety_floor import evaluate, signals_from_coach_inputs

    floor = evaluate(signals_from_coach_inputs(inputs))
    return {"level": floor["level"], "rules": floor["rules"]}


REFUSAL = {
    "morning": (
        "Rest today and don't train. What you reported needs care: give it time, and see a "
        "physiotherapist or doctor if it doesn't settle."
    ),
    "evening": (
        "Keep tomorrow free of training. What you reported needs care: give it time, and see "
        "a physiotherapist or doctor if it doesn't settle."
    ),
    "weekly": (
        "Don't add hard training next week. What you reported needs care: give it time, and "
        "see a physiotherapist or doctor if it doesn't settle."
    ),
    "block": (
        "Don't judge this block with a hard test. What you reported needs care: give it time, "
        "and see a physiotherapist or doctor if it doesn't settle."
    ),
}
FLAG = {
    "morning": (
        "Keep it easy: no hard training today. What you reported needs care; see a "
        "physiotherapist or doctor if it doesn't settle."
    ),
    "evening": (
        "Keep the next day easy: no hard training. What you reported needs care; see a "
        "physiotherapist or doctor if it doesn't settle."
    ),
    "weekly": (
        "Keep next week easy: no hard training. What you reported needs care; see a "
        "physiotherapist or doctor if it doesn't settle."
    ),
    "block": (
        "Don't use a hard test to judge this block. Keep it easy. See a physiotherapist "
        "or doctor if it doesn't settle."
    ),
}
# Rules about training load only: no complaint was reported, so the text says what it is
# and points to no physio or doctor.
LOAD_RULES = frozenset({"load_spike", "acwr_high", "tsb_deep"})
LOAD_FLAG = {
    "morning": "Your training load is rising fast: keep today easy, no hard training.",
    "evening": "Your training load is rising fast: keep the next day easy, no hard training.",
    "weekly": "Your training load is rising fast: keep next week easy, no hard training.",
    "block": "Your training load rose fast in this block: don't judge it with a hard test.",
}


def load_only(rules) -> bool:
    """True when every fired rule is about load, not about a complaint, pain or illness."""
    return bool(rules) and set(rules) <= LOAD_RULES


def target_date(review_day: date, kind: str) -> date:
    """Morning advice targets its day; evening advice targets the next day. A weekly review
    is stored on Monday and applies through that Sunday. A block review targets its end date."""
    if kind == "evening":
        return review_day + timedelta(days=1)
    if kind == "weekly":
        return rhythm.week_end(review_day)
    return review_day


def stored_safety_level(row: dict | None) -> str:
    """Level from the saved review row. Missing or unknown levels fail closed to flag."""
    if not row:
        return "ok"
    safety = row.get("safety")
    if not isinstance(safety, dict):
        return "flag"
    level = safety.get("level")
    if level in _SAFETY_LEVELS:
        return level
    return "flag"


def fixed_safety_summary(rt, kind: str, level: str, rules=None) -> str:
    return guard_text(rt, "", {"level": level, "rules": list(rules or [])}, kind=kind)


def display_for(rt, review_day: date, kind: str, ready: dict | None) -> dict:
    """How clients should render a saved review: current vs archive, advice vs safety-only."""
    today = today_in(rt.settings.timezone)
    target = target_date(review_day, kind)
    level = stored_safety_level(ready)
    rules = ((ready or {}).get("safety") or {}).get("rules") or []
    mode = "safety" if level in ("flag", "refuse") else "advice"
    # A coach that wrote advice has seen the load numbers: a load-only flag does not hide it.
    if mode == "safety" and load_only(rules) and (ready or {}).get("interpretation"):
        mode = "advice"
    # An archived review (its target day has passed) is history: no safety advice for then.
    if mode == "safety" and target < today:
        mode = "advice"
    safety_text = None
    if mode == "safety":
        safety_text = fixed_safety_summary(rt, kind, level, rules)
    return {
        "mode": mode,
        "target_date": target.isoformat(),
        "archive": target < today,
        "safety_text": safety_text,
    }


SAFE_ADVICE = {
    "en": {
        "flag": "Take it easy or rest today. Skip hard training.",
        "refuse": "Rest and avoid training today. Seek medical advice if the concern persists.",
        "evening_flag": "Take it easy or rest tomorrow. Skip hard training.",
        "evening_refuse": (
            "Keep tomorrow free of training. Seek medical advice if the concern persists."
        ),
    },
    "nl": {
        "flag": "Doe het rustig aan of neem rust. Sla zware training over.",
        "refuse": "Neem rust en train vandaag niet. Vraag medisch advies als de klacht aanhoudt.",
        "evening_flag": "Doe het morgen rustig aan of neem rust. Sla zware training over.",
        "evening_refuse": (
            "Houd morgen vrij van training. Vraag medisch advies als de klacht aanhoudt."
        ),
    },
}


def guard_text(rt, text: str, safety_state: dict, *, kind: str = "morning") -> str:
    """Use fixed advice under a safety restriction; never classify model prose by keywords."""
    level = safety_state.get("level")
    if level not in _SAFETY_LEVELS:
        level = "flag"
    # ease only softens the dashboard sentence. It does not replace the coach's words.
    if level in ("ok", "ease"):
        return text
    if kind in REFUSAL:
        tr = translator(_language(rt))
        if level == "refuse":
            return tr(REFUSAL[kind])
        if load_only(safety_state.get("rules")):
            return tr(LOAD_FLAG[kind])
        return tr(FLAG[kind])
    language = "nl" if _language(rt) == "nl" else "en"
    key = f"evening_{level}" if kind == "evening" else level
    return SAFE_ADVICE[language][key]


def guard_advice(rt, advice: dict, safety_state: dict, *, kind: str = "activity") -> dict:
    """Discard every model-authored field when a fixed safety rule fires."""
    level = safety_state.get("level")
    if level not in _SAFETY_LEVELS:
        level = "flag"
    if level in ("ok", "ease"):
        return advice
    safety_state = {**safety_state, "level": level}
    safe = {
        "summary": guard_text(rt, advice.get("summary", ""), safety_state, kind=kind),
        "caveat": None,
        "uncertainties": [],
        "evidence_sections": ["safety"],
    }
    if kind == "morning":
        safe.update(
            decision="rest" if safety_state["level"] == "refuse" else "take_it_easier",
            tomorrow=None,
            question=None,
            working=None,
            next_week=None,
            block_result=None,
        )
    elif kind in RHYTHM_KINDS or kind == "evening":
        safe.update(
            decision=None,
            tomorrow=None,
            question=None,
            working=None,
            next_week=None,
            block_result=None,
        )
    return safe


def current_safety(rt, day: date, *, conn=None, cache=None) -> dict:
    """Fixed-rule level for one calendar day, without reading activity assessments."""
    if conn is not None and cache is not None:
        return cache.capture(rt, conn, day, "morning", safety_only=True)["inputs"]["safety"]
    with rt.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        return _capture(rt, conn, day, "morning", safety_only=True)["inputs"]["safety"]


def _language(rt) -> str | None:
    from vibesynergy.portal import prefs

    return prefs.load(rt.settings.data_dir).get("language")


def _day_activities(rt, conn, day: date) -> list[dict]:
    """The day's own sessions and the same saved assessment the portal reads.

    ``stored_for_activity`` is read-only: this does not prepare work or call a model.
    """
    rows = conn.execute(
        select(
            activity_unified.c.group_id,
            activity_unified.c.sources,
            activity_unified.c.name,
            activity_unified.c.sport,
            activity_unified.c.moving_s,
            activity_unified.c.distance_m,
        )
        .where(
            activity_unified.c.subject_id == SUBJECT,
            activity_unified.c.local_date == day,
            assessments.own_training(),
        )
        .order_by(activity_unified.c.start_utc)
    ).mappings()
    found = []
    for r in rows:
        ref = next(iter(r["sources"] or []), None)
        saved = (
            assessments.stored_for_activity(rt, ref)
            if ref
            else {"state": None, "input_version": None, "advice": None}
        )
        found.append(
            {
                "id": r["group_id"],
                "name": r["name"],
                "sport": r["sport"],
                "moving_s": r["moving_s"],
                "distance_m": r["distance_m"],
                "assessment_state": saved["state"],
                "assessment_version": saved["input_version"],
                "assessment": saved["advice"],
            }
        )
    return found


def _plan_items(items: list[dict], day: date) -> list[dict]:
    keys = ("title", "sport", "status", "kind", "is_race", "primary", "source", "source_id")
    return [
        {**{k: i[k] for k in keys if k in i}, "timing": _timing(i)}
        for i in items
        if i["date"] == str(day) and i["kind"] in plan.TRAINING_KINDS | {"commitment"}
    ]


def _timing(item: dict) -> dict:
    extra = item.get("extra") or {}
    return {k: extra[k] for k in ("start", "end", "all_day") if k in extra}


def _capture(
    rt,
    conn,
    day: date,
    kind: str,
    *,
    safety_only: bool = False,
    profile: dict | None = None,
    race: dict | None = None,
) -> dict:
    reader = snapshot.SnapshotReader(conn)
    span = Window(day - timedelta(days=CONTEXT_DAYS - 1), day)
    calendar = plan.plan(
        reader, Window(day, day + timedelta(days=1)), day, include_sync_status=False
    )
    daily = {
        (r.date, r.metric): r.value
        for r in conn.execute(
            select(daily_unified.c.date, daily_unified.c.metric, daily_unified.c.value).where(
                daily_unified.c.subject_id == SUBJECT,
                daily_unified.c.date.between(span.start, span.end),
                daily_unified.c.metric.in_(RECOVERY),
            )
        )
    }
    recovery = {m: daily[(day, m)] for m in RECOVERY if (day, m) in daily}
    recent = training.volume(
        reader,
        Window(day - timedelta(days=6), day),
        granularity="day",
        by="all",
        include_coaching=False,
    )["totals"].get("all", {})
    previous = training.volume(
        reader,
        Window(day - timedelta(days=34), day - timedelta(days=7)),
        granularity="day",
        by="all",
        include_coaching=False,
    )["totals"].get("all", {})
    recent_s, usual_s = recent.get("moving_s", 0), previous.get("moving_s", 0) / 4
    # One load model everywhere: intervals.icu's.
    load = training.training_load(
        reader,
        Window(day - timedelta(days=34), day),
        include_coaching=False,
        load="vibesynergy",
        athlete=rt.settings.athlete,
        raw=getattr(rt, "raw", None),
        parquet=getattr(rt, "parquet", None),
    )
    if profile is None:
        profile = coach.snapshot(reader, raw=rt.raw)
    if race is None:
        race = coach.coaching_race_context(reader, day, raw=rt.raw, profile=profile)
    injuries = journal.injuries(reader, day)
    notes = [
        {"text": n["text"], "tags": n["tags"]}
        for d in (day - timedelta(days=1), day)
        for n in journal.day_journal(reader, d)["notes"]
    ]
    checkin = (journal.context(reader, day) or {}).get("checkin")
    pending = [
        d["dataset"] for d in views.sync_status(reader)["datasets"] if d["processing"]["pending"]
    ]
    inputs = snapshot.json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "kind": kind,
            "date": day,
            "recovery": recovery,
            "recovery_landed": any(m in recovery for m in ("sleep.score", "hrv.night_avg")),
            "hrv_baseline": series.baseline(reader, "hrv.night_avg", day, window_days=41),
            "week": {
                "last_7d_moving_s": recent_s,
                "usual_7d_moving_s": round(usual_s),
                "planned_7d_moving_s": plan.planned_moving_s(
                    reader, Window(day - timedelta(days=6), day)
                ),
                "volume_ramp_pct": round(100 * (recent_s / usual_s - 1), 1) if usual_s else None,
                "last_7d_sessions": recent.get("count", 0),
                "ctl_ramp_7d": load["current"]["ctl_ramp_7d"]
                if (load.get("hr_profile") or {}).get("max_hr")
                else None,
            },
            "plan_today": _plan_items(calendar["items"], day),
            "plan_tomorrow": _plan_items(calendar["items"], day + timedelta(days=1)),
            "done_today": [] if safety_only else _day_activities(rt, conn, day),
            **{key: race[key] for key in ("next_race", "plan_phase", "supporting")},
            "injuries": [
                {
                    "title": i["title"],
                    "latest_pain": i["latest_pain"],
                    "flags": [{"level": f["level"], "rule": f["rule"]} for f in i["flags"]],
                }
                for i in injuries
            ],
            "notes": notes,
            "tags": sorted({t for n in notes for t in n["tags"]}),
            "checkin": checkin,
            "coach": profile,
            "processing_pending": pending,
        }
    )
    inputs["safety"] = safety(inputs)
    return {"input_version": evidence.digest(_semantic(inputs)), "inputs": inputs}


def _semantic(inputs: dict) -> dict:
    """What decides the version: shared coaching state and evidence, never receipt times."""
    profile = inputs["coach"]
    semantic = {
        **{k: v for k, v in inputs.items() if k not in ("coach", "processing_pending")},
        "prompt_version": INPUT_PROMPT_VERSION,
        "coach": {
            "documents": {
                name: revision["effective"] for name, revision in profile["documents"].items()
            },
        },
    }
    # Only the assessment versions count, so a later review follows the same advice.
    if "done_today" in inputs:
        semantic["done_today"] = [
            {k: v for k, v in a.items() if k != "assessment"} for a in inputs["done_today"]
        ]
    semantic.update(rhythm.semantic_lists(inputs))
    return snapshot.semantic(semantic)


class _ReadCache:
    """One coach profile, race context and capture per ``read()`` request."""

    def __init__(self, rt):
        self.rt = rt
        self._profile = None
        self._race: dict[date, dict] = {}
        self._capture: dict[tuple, dict] = {}

    def profile(self, conn):
        if self._profile is None:
            reader = snapshot.SnapshotReader(conn)
            self._profile = coach.snapshot(reader, raw=self.rt.raw)
        return self._profile

    def race(self, conn, day: date) -> dict:
        if day not in self._race:
            reader = snapshot.SnapshotReader(conn)
            self._race[day] = coach.coaching_race_context(
                reader, day, raw=self.rt.raw, profile=self.profile(conn)
            )
        return self._race[day]

    def capture(self, rt, conn, day: date, kind: str, *, safety_only: bool = False) -> dict:
        key = (day, kind, safety_only)
        if key in self._capture:
            return self._capture[key]
        profile = self.profile(conn)
        if kind in RHYTHM_KINDS:
            today = today_in(rt.settings.timezone)
            if kind == "weekly":
                monday = rhythm.week_start(day)
                end = rhythm.week_end(monday)
                through = min(end, today) if today >= monday else monday
                race = self.race(conn, through)
            else:
                race = self.race(conn, day)
            snap = _capture_rhythm(rt, conn, day, kind, profile=profile, race=race)
        else:
            snap = _capture(
                rt,
                conn,
                day,
                kind,
                safety_only=safety_only,
                profile=profile,
                race=self.race(conn, day),
            )
        self._capture[key] = snap
        return snap


def _capture_rhythm(
    rt,
    conn,
    day: date,
    kind: str,
    *,
    profile: dict | None = None,
    race: dict | None = None,
) -> dict:
    today = today_in(rt.settings.timezone)
    shared = {"profile": profile, "race": race}
    if kind == "weekly":
        monday = rhythm.week_start(day)
        inputs = rhythm.weekly_inputs(rt, conn, monday, today, **shared)
        stored = monday
    else:
        inputs = rhythm.block_inputs(rt, conn, day, **shared)
        stored = day
    inputs = snapshot.json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "kind": kind,
            "date": stored,
            **inputs,
        }
    )
    inputs["safety"] = safety(inputs) if "injuries" in inputs else {"level": "ok", "rules": []}
    inputs.setdefault("processing_pending", [])
    return {"input_version": evidence.digest(_semantic(inputs)), "inputs": inputs}


def capture(
    rt,
    day: date,
    kind: str,
    *,
    conn=None,
    cache: _ReadCache | None = None,
) -> dict:
    if kind not in ALL_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ALL_KINDS)}")
    if cache is not None and conn is not None:
        return cache.capture(rt, conn, day, kind)
    with rt.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        if kind in RHYTHM_KINDS:
            return _capture_rhythm(rt, conn, day, kind)
        return _capture(rt, conn, day, kind)


def _public(row, *, current: bool) -> dict:
    out = snapshot.json_safe(
        {
            k: row[k]
            for k in ("id", "state", "interpretation", "model", "previous_id", "changed", "error")
        }
    )
    out.update(
        completed_at=snapshot.json_safe(row["completed_at"]),
        safety=row["safety"],
        current=current,
        input_version=row["input_version"],
        profile_version=row["inputs"]["coach"]["version"],
    )
    return out


def _with_live_safety(
    rt,
    kind: str,
    entry: dict,
    *,
    conn=None,
    cache: _ReadCache | None = None,
) -> dict:
    """Add today's safety line when fixed rules are not ok.

    An external coach entry keeps its own words. The portal shows the safety line beside
    them. A review written by the in-app model still drops that model's wording.
    """
    display = entry["display"]
    advice = entry["advice"]
    if display.get("archive") or not advice or not advice.get("interpretation"):
        return entry
    today = today_in(rt.settings.timezone)
    target = date.fromisoformat(display["target_date"])
    if today > target:
        return entry
    live = current_safety(rt, today, conn=conn, cache=cache)
    if live["level"] not in ("flag", "refuse"):
        return entry
    if load_only(live.get("rules")):
        return entry  # the coach's advice stays; load alone is no reason to replace it
    phrase = "morning" if kind == "evening" and today >= target else kind
    text = fixed_safety_summary(rt, phrase, live["level"], live.get("rules"))
    if entry.get("source") == "coach_decision_log":
        return {**entry, "display": {**display, "mode": "safety", "safety_text": text}}
    return {
        **entry,
        "display": {**display, "mode": "safety", "safety_text": text},
        "advice": {
            **advice,
            "interpretation": guard_advice(rt, advice["interpretation"], live, kind=phrase),
        },
    }


def _daily_entry_row(conn, day: date, kind: str):
    return (
        conn.execute(
            select(coach_decision_log)
            .where(
                coach_decision_log.c.subject_id == SUBJECT,
                coach_decision_log.c.for_date == day,
                coach_decision_log.c.entry_type == kind,
            )
            .order_by(desc(coach_decision_log.c.id))
            .limit(1)
        )
        .mappings()
        .first()
    )


def _entry_versions(meta: dict) -> dict:
    return {
        key: meta[key]
        for key in (
            "model_id",
            "prompt_version",
            "context_version",
            "policy_version",
            "evidence_version",
            "profile_version",
        )
        if meta.get(key) is not None
    }


def _coach_daily_view(
    rt,
    day: date,
    kind: str,
    *,
    profile_version,
    conn,
    cache: _ReadCache | None,
) -> dict | None:
    """Morning/evening from coach_decision_log when the AI coach wrote a typed entry."""
    from vibesynergy.query import coach_workouts

    row = _daily_entry_row(conn, day, kind)
    if row is None:
        return None
    coach_snap = coach.snapshot(rt.engine, subject_id=SUBJECT, timezone=rt.settings.timezone)
    enriched = coach_workouts.enrich_coach_entry(
        rt,
        coach_workouts.coach_row(dict(row)),
        coach_snap,
        subject_id=SUBJECT,
    )
    snap = cache.capture(rt, conn, day, kind) if cache is not None else capture(rt, day, kind)
    freshness = enriched["freshness"]
    needs_revision = freshness == "needs_revision"
    if freshness == "current":
        state = "ready"
    elif freshness == "stale":
        state = "stale"
    else:
        state = "superseded"
    meta = enriched.get("meta") or {}
    interpretation = {
        "summary": enriched["advice_text"],
        "decision": None,
        "tomorrow": None,
        "caveat": enriched.get("caveat") or None,
        "question": None,
        "working": None,
        "next_week": None,
        "block_result": None,
        "uncertainties": [],
        "evidence_sections": ["safety"] if enriched.get("advice_mode") == "safety" else [],
    }
    if enriched.get("advice_mode") == "safety" and enriched.get("entry_text"):
        meta = {**meta, "coach_text": enriched["entry_text"]}
    display_row = None
    level = snap["inputs"]["safety"]["level"]
    if level in ("flag", "refuse") or enriched.get("advice_mode") == "safety":
        display_row = {"safety": snap["inputs"]["safety"]}
    elif state == "ready":
        display_row = {"interpretation": interpretation, "safety": snap["inputs"]["safety"]}
    advice = {
        "id": enriched["id"],
        "state": "ready",
        "interpretation": interpretation,
        "model": meta.get("model_id"),
        "previous_id": None,
        "changed": [],
        "error": None,
        "completed_at": enriched.get("at"),
        "safety": snap["inputs"]["safety"],
        "current": freshness == "current",
        "input_version": snap["input_version"],
        "profile_version": meta.get("profile_version") or profile_version,
    }
    return {
        "state": state,
        "needs_revision": needs_revision,
        "source": "coach_decision_log",
        "author": enriched["author"],
        "versions": _entry_versions(meta),
        "input_version": snap["input_version"],
        "advice": advice,
        "display": display_for(rt, day, kind, display_row),
        "pending_error": None,
        "safety": snap["inputs"]["safety"],
        "period": snap["inputs"].get("period"),
        "comparison": snap["inputs"].get("comparison"),
        "log": [],
        "coach_meta": meta,
    }


def _deterministic_without_coach(
    rt,
    day: date,
    kind: str,
    *,
    profile_version,
    conn,
    cache: _ReadCache | None,
) -> dict:
    """Narrative worker off and no coach entry: safety rules only, no LLM surface."""
    snap = cache.capture(rt, conn, day, kind) if cache is not None else capture(rt, day, kind)
    level = snap["inputs"]["safety"]["level"]
    display_row = {"safety": snap["inputs"]["safety"]} if level in ("flag", "refuse") else None
    return {
        "state": "no_coach",
        "needs_revision": False,
        "source": "deterministic",
        "author": None,
        "versions": {},
        "input_version": snap["input_version"],
        "advice": None,
        "display": display_for(rt, day, kind, display_row),
        "pending_error": None,
        "safety": snap["inputs"]["safety"],
        "period": snap["inputs"].get("period"),
        "comparison": snap["inputs"].get("comparison"),
        "log": [],
    }


def _view(
    rt,
    day: date,
    kind: str,
    *,
    profile_version,
    conn=None,
    cache: _ReadCache | None = None,
) -> dict:
    """Coach entry for this day and kind, or the safety floor when there is none.

    Reading stores nothing and does not use internal reviews.
    """
    if conn is None:
        with rt.engine.connect() as owned:
            owned.exec_driver_sql("BEGIN")
            return _view(rt, day, kind, profile_version=profile_version, conn=owned, cache=cache)
    written = _coach_daily_view(
        rt, day, kind, profile_version=profile_version, conn=conn, cache=cache
    )
    if written is not None:
        return written
    return _deterministic_without_coach(
        rt, day, kind, profile_version=profile_version, conn=conn, cache=cache
    )


def _block_unavailable(reason: str, **extra) -> dict:
    boundary = extra.get("boundary")
    return {
        "state": "unavailable",
        "reason": reason,
        "boundary": boundary,
        "plan_name": extra.get("plan_name"),
        "active_until": extra.get("active_until"),
        "needs_revision": False,
        "input_version": None,
        "advice": None,
        "display": {
            "mode": "advice",
            "target_date": boundary,
            "archive": False,
            "safety_text": None,
        },
        "pending_error": None,
        "safety": {"level": "ok", "rules": []},
        "period": None,
        "comparison": None,
        "log": [],
    }


def _block_view(
    rt,
    day: date,
    *,
    profile_version,
    conn=None,
    cache: _ReadCache | None = None,
) -> dict:
    if conn is None:
        with rt.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN")
            rows = rhythm.plans(conn)
    else:
        rows = rhythm.plans(conn)
    chosen = rhythm.boundary_on(rows, day) or rhythm.latest_ended(rows, day)
    running = rhythm.active_plan(rows, day)
    active_until = (
        running["end_date"].isoformat()
        if running and (not chosen or running["end_date"] != chosen.get("end_date"))
        else None
    )
    if chosen is None:
        if running:
            return _block_unavailable(
                "not_yet",
                boundary=running["end_date"].isoformat(),
                plan_name=running.get("name"),
                active_until=running["end_date"].isoformat(),
            )
        return _block_unavailable("no_block")
    view = _view(
        rt,
        chosen["end_date"],
        "block",
        profile_version=profile_version,
        conn=conn,
        cache=cache,
    )
    view.update(
        reason=None,
        boundary=chosen["end_date"].isoformat(),
        plan_name=chosen.get("name"),
        active_until=active_until,
    )
    return view


def read(rt, day: date | None = None) -> dict:
    """Morning, evening, weekly and block views from coach entries and safety.

    Never creates work or calls a model. Last week's coach note is included when this
    week has none yet.
    """
    day = day or today_in(rt.settings.timezone)
    profile = coach.state(rt.engine, "policy")["active"]
    cache = _ReadCache(rt)
    with rt.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        binding = coach.profile_binding(cache.profile(conn))
        version = binding["profile_version"]
        ctx = {"conn": conn, "cache": cache}
        out = {
            "date": day.isoformat(),
            "profile_active": bool(profile),
            "profile_version": version,
            "active_profile": binding,
            "automatic": False,
            "narrative_enabled": False,
            "contract_version": 1,
        }
        for kind in KINDS:
            out[kind] = _with_live_safety(
                rt, kind, _view(rt, day, kind, profile_version=version, **ctx), **ctx
            )
        monday = rhythm.week_start(day)
        out["weekly"] = _with_live_safety(
            rt, "weekly", _view(rt, monday, "weekly", profile_version=version, **ctx), **ctx
        )
        out["weekly_previous"] = None
        if out["weekly"]["advice"] is None:
            prev_monday = monday - timedelta(days=7)
            previous = _view(rt, prev_monday, "weekly", profile_version=version, **ctx)
            if previous.get("advice"):
                out["weekly_previous"] = previous
        out["block"] = _with_live_safety(
            rt, "block", _block_view(rt, day, profile_version=version, **ctx), **ctx
        )
    return out
