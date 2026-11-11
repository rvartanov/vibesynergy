"""Weekly review and end-of-block evaluation on the shared coaching evidence.

These are further kinds of the stored daily review, not a second coach. A weekly review
says whether the plan is working and proposes a focus for next week. A block review runs
when a training plan reaches its end date and judges that block only from recorded test
efforts (a Cooper test, a time trial). Calendar commitments are context, never training load.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import assessments, coach, journal, plan, series, snapshot, training, views
from vibesynergy.store.schema import activity_unified, daily_unified, training_plan

SUBJECT = 1
RECOVERY = assessments.METRICS
# A title is a test effort only when it names one. A session merely called "Test" is not.
_TEST = re.compile(
    r"\b(cooper(?:\s+test)?|time[\s-]?trial|tijdrit|tijdproef|ftp(?:\s+test)?)\b",
    re.IGNORECASE,
)
# Two efforts count as the same test when their distances are this close. Otherwise we do
# not invent a comparison between, for example, a kilometre and a five-kilometre effort.
_SAME_DISTANCE = 0.05
# A pace change smaller than this is "held", so noise is not called an improvement.
_PACE_CHANGE = 0.01


def week_start(day: date) -> date:
    """Monday of the ISO week that contains ``day``."""
    return day - timedelta(days=day.weekday())


def week_end(monday: date) -> date:
    return monday + timedelta(days=6)


def is_test_title(title: str | None) -> bool:
    return bool(title and _TEST.search(title))


def compare_efforts(efforts: list[dict]) -> str:
    """improved, held, short, single or no_test. Never invents a time or a distance."""
    recorded = [e for e in efforts if e.get("recorded")]
    if not recorded:
        return "no_test"
    usable = [
        e
        for e in recorded
        if isinstance(e.get("distance_m"), (int, float))
        and isinstance(e.get("moving_s"), (int, float))
        and e["distance_m"] > 0
        and e["moving_s"] > 0
    ]
    usable.sort(key=lambda e: e.get("date") or "")
    if len(usable) < 2 or not _similar_distance(usable[0], usable[-1]):
        return "single"
    first = usable[0]["moving_s"] / usable[0]["distance_m"]
    last = usable[-1]["moving_s"] / usable[-1]["distance_m"]
    if last < first * (1 - _PACE_CHANGE):
        return "improved"
    if last > first * (1 + _PACE_CHANGE):
        return "short"
    return "held"


def _similar_distance(first: dict, last: dict) -> bool:
    left, right = first["distance_m"], last["distance_m"]
    return abs(left - right) / max(left, right) <= _SAME_DISTANCE


def plans(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            select(training_plan).where(training_plan.c.subject_id == SUBJECT)
        ).mappings()
    ]


def boundary_on(rows: list[dict], day: date) -> dict | None:
    matches = [row for row in rows if row.get("end_date") == day]
    if not matches:
        return None
    return max(matches, key=lambda row: (row.get("start_date") or date.min, row.get("name") or ""))


def latest_ended(rows: list[dict], day: date) -> dict | None:
    ended = [row for row in rows if row.get("end_date") and row["end_date"] <= day]
    if not ended:
        return None
    return max(ended, key=lambda row: (row["end_date"], row.get("start_date") or date.min))


def active_plan(rows: list[dict], day: date) -> dict | None:
    running = [
        row
        for row in rows
        if row.get("end_date")
        and row["end_date"] > day
        and (row.get("start_date") is None or row["start_date"] <= day)
        and (row.get("status") or "").lower() not in plan.ENDED
    ]
    if not running:
        return None
    return min(running, key=lambda row: row["end_date"])


def fitness_trend(markers: dict) -> list[dict]:
    """Recorded changes only. One point is not a trend, and a missing marker is not zero."""
    trend = []
    for metric, changes in markers.items():
        if not changes:
            continue
        if len(changes) == 1:
            trend.append({"metric": metric, "from": changes[0], "to": None})
        else:
            trend.append({"metric": metric, "from": changes[0], "to": changes[-1]})
    return trend


def activities_between(rt, conn, start: date, end: date) -> list[dict]:
    """Own sessions in the window and the same saved assessment the portal reads.

    ``stored_for_activity`` is read-only: this does not prepare work or call a model.
    """
    rows = conn.execute(
        select(
            activity_unified.c.group_id,
            activity_unified.c.local_date,
            activity_unified.c.sources,
            activity_unified.c.name,
            activity_unified.c.sport,
            activity_unified.c.moving_s,
            activity_unified.c.distance_m,
        )
        .where(
            activity_unified.c.subject_id == SUBJECT,
            activity_unified.c.local_date.between(start, end),
            assessments.own_training(),
        )
        .order_by(activity_unified.c.local_date, activity_unified.c.start_utc)
    ).mappings()
    refs = []
    row_list = []
    seen: set[str] = set()
    for row in rows:
        if row["group_id"] in seen:
            continue
        seen.add(row["group_id"])
        row_list.append(row)
        refs.append(next(iter(row["sources"] or []), None))
    summaries = assessments.stored_summaries_for_refs(rt, conn, SUBJECT, [r for r in refs if r])
    empty = {"state": "missing", "input_version": None, "advice": None}
    found: list[dict] = []
    for row, ref in zip(row_list, refs, strict=True):
        saved = summaries.get(ref, empty) if ref else empty
        found.append(
            {
                "id": row["group_id"],
                "date": row["local_date"].isoformat(),
                "name": row["name"],
                "sport": row["sport"],
                "moving_s": row["moving_s"],
                "distance_m": row["distance_m"],
                "assessment_state": saved["state"],
                "assessment_version": saved["input_version"],
                "assessment": saved["advice"],
            }
        )
    return found


def _timing(item: dict) -> dict:
    extra = item.get("extra") or {}
    return {k: extra[k] for k in ("start", "end", "all_day") if k in extra}


def _sessions(items: list[dict], start: date, end: date, *, kinds: set[str]) -> list[dict]:
    out = []
    for item in items:
        if item["kind"] not in kinds:
            continue
        day = date.fromisoformat(item["date"])
        if not start <= day <= end:
            continue
        out.append(
            {
                "date": item["date"],
                "title": item.get("title"),
                "sport": item.get("sport"),
                "status": item.get("status"),
                "kind": item["kind"],
                "timing": _timing(item),
            }
        )
    return out


def _adherence(calendar: dict) -> dict:
    planned = calendar["adherence"]["planned"]
    done = calendar["adherence"]["done"]
    return {
        "planned": planned,
        "done": done,
        "ratio": round(done / planned, 2) if planned else None,
    }


def _signals(
    rt,
    conn,
    day: date,
    *,
    profile: dict | None = None,
    race: dict | None = None,
) -> dict:
    """Recovery, load and diary for the safety rules. Missing metrics stay absent."""
    reader = snapshot.SnapshotReader(conn)
    if profile is None:
        profile = coach.snapshot(reader, raw=rt.raw)
    if race is None:
        race = coach.coaching_race_context(reader, day, raw=rt.raw, profile=profile)
    span = Window(day - timedelta(days=13), day)
    daily = {
        (row.date, row.metric): row.value
        for row in conn.execute(
            select(daily_unified.c.date, daily_unified.c.metric, daily_unified.c.value).where(
                daily_unified.c.subject_id == SUBJECT,
                daily_unified.c.date.between(span.start, span.end),
                daily_unified.c.metric.in_(RECOVERY),
            )
        )
    }
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
    notes = [
        {"text": note["text"], "tags": note["tags"]}
        for journal_day in (day - timedelta(days=1), day)
        for note in journal.day_journal(reader, journal_day)["notes"]
    ]
    injuries = journal.injuries(reader, day)
    return {
        "recovery": {metric: daily[(day, metric)] for metric in RECOVERY if (day, metric) in daily},
        "recovery_landed": any(
            (day, metric) in daily for metric in ("sleep.score", "hrv.night_avg")
        ),
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
        "injuries": [
            {
                "title": item["title"],
                "latest_pain": item["latest_pain"],
                "flags": [{"level": flag["level"], "rule": flag["rule"]} for flag in item["flags"]],
            }
            for item in injuries
        ],
        "notes": notes,
        "tags": sorted({tag for note in notes for tag in note["tags"]}),
        "checkin": (journal.context(reader, day) or {}).get("checkin"),
        "coach": profile,
        "processing_pending": [
            row["dataset"]
            for row in views.sync_status(reader)["datasets"]
            if row["processing"]["pending"]
        ],
        **{key: race[key] for key in ("next_race", "plan_phase", "supporting")},
    }


def _tests(activities: list[dict], planned: list[dict]) -> tuple[list[dict], list[dict]]:
    """Recorded test efforts, plus planned tests that have no recorded session."""
    recorded: dict[str, dict] = {}
    missed: list[dict] = []
    for activity in activities:
        if is_test_title(activity.get("name")):
            recorded[activity["id"]] = {**activity, "recorded": True, "planned_title": None}
    for item in planned:
        if not is_test_title(item.get("title")):
            continue
        family = plan.family(item.get("sport"))
        match = next(
            (
                activity
                for activity in activities
                if activity["date"] == item["date"] and plan.family(activity.get("sport")) == family
            ),
            None,
        )
        if match:
            recorded[match["id"]] = {
                **match,
                "recorded": True,
                "planned_title": item.get("title"),
            }
        else:
            missed.append(
                {"date": item["date"], "title": item.get("title"), "sport": item.get("sport")}
            )
    efforts = sorted(recorded.values(), key=lambda item: item["date"])
    return efforts, missed


def weekly_inputs(
    rt,
    conn,
    monday: date,
    today: date,
    *,
    profile: dict | None = None,
    race: dict | None = None,
) -> dict:
    """One week of shared evidence. ``monday`` is the stored identity of that week."""
    reader = snapshot.SnapshotReader(conn)
    end = week_end(monday)
    through = min(end, today) if today >= monday else monday
    if profile is None:
        profile = coach.snapshot(reader, raw=rt.raw)
    if race is None:
        race = coach.coaching_race_context(reader, through, raw=rt.raw, profile=profile)
    calendar = plan.plan(reader, Window(monday, end), through, include_sync_status=False)
    nxt = monday + timedelta(days=7)
    upcoming = plan.plan(
        reader, Window(nxt, nxt + timedelta(days=6)), through, include_sync_status=False
    )
    markers = training.fitness(reader, Window(monday - timedelta(days=41), through))["markers"]
    done = activities_between(rt, conn, monday, through)
    return snapshot.json_safe(
        {
            "period": {"start": monday, "end": end, "through": through},
            "adherence": _adherence(calendar),
            "fitness_trend": fitness_trend(markers),
            "planned": _sessions(calendar["items"], monday, end, kinds=plan.TRAINING_KINDS),
            "commitments": _sessions(calendar["items"], monday, end, kinds={"commitment"}),
            "activities": done,
            "next_week_plan": _sessions(
                upcoming["items"], nxt, nxt + timedelta(days=6), kinds=plan.TRAINING_KINDS
            ),
            "next_week_commitments": _sessions(
                upcoming["items"], nxt, nxt + timedelta(days=6), kinds={"commitment"}
            ),
            "previous_week": _previous_weekly(conn, monday),
            **_signals(rt, conn, through, profile=profile, race=race),
        }
    )


def block_inputs(
    rt,
    conn,
    boundary: date,
    *,
    profile: dict | None = None,
    race: dict | None = None,
) -> dict:
    """The plan that ends on ``boundary``, judged from recorded tests in that plan."""
    reader = snapshot.SnapshotReader(conn)
    if profile is None:
        profile = coach.snapshot(reader, raw=rt.raw)
    if race is None:
        race = coach.coaching_race_context(reader, boundary, raw=rt.raw, profile=profile)
    chosen = boundary_on(plans(conn), boundary) or latest_ended(plans(conn), boundary)
    if chosen is None:
        return snapshot.json_safe({"block": None, "comparison": "no_test", "test_efforts": []})
    start = chosen.get("start_date") or (boundary - timedelta(days=84))
    calendar = plan.plan(reader, Window(start, boundary), boundary, include_sync_status=False)
    done = activities_between(rt, conn, start, boundary)
    planned = _sessions(calendar["items"], start, boundary, kinds=plan.TRAINING_KINDS)
    efforts, missed = _tests(done, planned)
    return snapshot.json_safe(
        {
            "period": {"start": start, "end": boundary},
            "block": {
                "name": chosen.get("name"),
                "source": chosen.get("source"),
                "source_id": chosen.get("source_id"),
                "start": start,
                "end": boundary,
                "at_boundary": chosen.get("end_date") == boundary,
            },
            "adherence": _adherence(calendar),
            "fitness_trend": fitness_trend(
                training.fitness(reader, Window(start, boundary))["markers"]
            ),
            "planned": planned,
            "commitments": _sessions(calendar["items"], start, boundary, kinds={"commitment"}),
            "activities": done,
            "test_efforts": efforts,
            "missed_tests": missed,
            "comparison": compare_efforts(efforts),
            **_signals(rt, conn, boundary, profile=profile, race=race),
        }
    )


def _previous_weekly(conn, monday: date) -> dict | None:
    """Last week's coach note, when one was written. Internal reviews are not read."""
    from sqlalchemy import desc

    from vibesynergy.store.schema import coach_decision_log

    row = (
        conn.execute(
            select(coach_decision_log)
            .where(
                coach_decision_log.c.subject_id == SUBJECT,
                coach_decision_log.c.for_date == monday - timedelta(days=7),
                coach_decision_log.c.entry_type == "weekly",
            )
            .order_by(desc(coach_decision_log.c.id))
            .limit(1)
        )
        .mappings()
        .first()
    )
    if not row or not row["text"]:
        return None
    return {"working": None, "summary": row["text"], "safety": None}


def semantic_lists(inputs: dict) -> dict:
    """Drop saved assessment prose so a review follows the assessment version, not a second copy."""
    out = {}
    if "activities" in inputs:
        out["activities"] = [
            {k: v for k, v in activity.items() if k != "assessment"}
            for activity in inputs["activities"]
        ]
    if "test_efforts" in inputs:
        out["test_efforts"] = [
            {k: v for k, v in effort.items() if k != "assessment"}
            for effort in inputs["test_efforts"]
        ]
    return out
