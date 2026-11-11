"""The training calendar: coaching plans, planned workouts and events (races), and how the
planned sessions compare with what was done. Used by the MCP tool vibesynergy_plan and the
portal's Dashboard.

From the first intervals.icu plan workout on, the intervals.icu calendar is the plan.
A Garmin scheduled workout that intervals.icu pushed (same day, sport family and name, or a
title carrying the plan's external-id tag such as ``(lake27)``) is folded into that workout
as ``garmin_copies``. Any other Garmin workout from that day on is not in the plan: it goes
to ``not_in_plan`` and is never counted as planned. Earlier days keep the Garmin calendar
as the plan."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, or_, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import coaching
from vibesynergy.query.race_priority import effective_priority, stored_priorities
from vibesynergy.query.safety_floor import title_band
from vibesynergy.query.views import sync_status
from vibesynergy.store.schema import activity_unified, planned_item, training_plan

INTERVALS = "intervals_icu"

# Sport families, to match a planned session with the activity that did it.
_FAMILIES = (
    ("run", ("run", "trail", "treadmill")),
    ("ride", ("cycl", "bik", "ride")),
    ("swim", ("swim",)),
    ("strength", ("strength", "training", "fitness", "gym", "yoga", "pilates", "mobility")),
    ("walk", ("walk", "hik")),
)
TRAINING_KINDS = {"workout", "plan_workout"}
ENDED = {"completed", "cancelled", "canceled", "ended", "abandoned"}
TAPER_DAYS = 14
# A matched session is "changed" when duration or distance is off by more than this,
# or when both sides name an intensity and those names differ.
_CHANGE = 0.25
_INTENSITY = {"easy", "moderate", "hard", "race"}
_KM = re.compile(r"(\d+(?:[.,]\d+)?)\s*km\b", re.IGNORECASE)
_MINUTES = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:minutes|minute|minuten|min)\b", re.IGNORECASE)
_HOURS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:hours|hour|uur)\b", re.IGNORECASE)
# The sport key a plan workout gets, so it reads like a Garmin one.
_SPORT = {
    "run": "running",
    "ride": "cycling",
    "swim": "swimming",
    "strength": "strength_training",
    "walk": "walking",
}
_DATE_HEAD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@lru_cache(maxsize=128)
def family(sport: str | None) -> str | None:
    text = (sport or "").lower()
    return next((name for name, words in _FAMILIES if any(w in text for w in words)), None)


def calendar_week_window(today: date) -> Window:
    """Monday through Sunday of the week that contains ``today``."""
    monday = today - timedelta(days=today.weekday())
    return Window(monday, monday + timedelta(days=6))


def _amount(text: str) -> float:
    return float(text.replace(",", "."))


def planned_distance_m(item: dict[str, Any]) -> float | None:
    """Metres on the plan. A title such as ``Tempoloop 13.5 km`` counts when no field is set."""
    extra = item.get("extra") or {}
    for key in ("distance_m", "distanceInMeters"):
        value = extra.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    distance = extra.get("distance")
    # Garmin's calendar stores metres. A small number is ambiguous, so the title is used.
    if isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance >= 100:
        return float(distance)
    match = _KM.search(item.get("title") or "")
    if match:
        return _amount(match.group(1)) * 1000
    return None


def planned_duration_s(item: dict[str, Any]) -> float | None:
    """Seconds on the plan. ``duration`` is seconds when it is at least five minutes."""
    extra = item.get("extra") or {}
    for key in ("duration_s", "estimatedDurationInSecs", "durationInSecs"):
        value = extra.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    duration = extra.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 300:
        return float(duration)
    title = item.get("title") or ""
    if match := _MINUTES.search(title):
        return _amount(match.group(1)) * 60
    if match := _HOURS.search(title):
        return _amount(match.group(1)) * 3600
    return None


def planned_intensity(item: dict[str, Any]) -> str | None:
    """easy / moderate / hard / race when the plan says so. An ordinary title is left unset."""
    extra = item.get("extra") or {}
    raw = extra.get("intensity")
    if isinstance(raw, str) and raw.strip().lower() in _INTENSITY:
        return raw.strip().lower()
    band = title_band(item.get("title"))
    if band in {"hard", "easy"}:
        return band
    return None


def _relative_change(planned: float | None, actual: float | None) -> bool:
    if planned is None or actual is None or planned <= 0:
        return False
    return abs(float(actual) - float(planned)) / float(planned) > _CHANGE


def departs_from_plan(item: dict[str, Any], activity: dict[str, Any]) -> bool:
    """True when the matched activity is not the session that was planned.

    Duration or distance off by more than 25%, or a different named intensity.
    A missing number is not a difference.
    """
    actual_duration = activity.get("moving_s") or activity.get("elapsed_s")
    if _relative_change(planned_duration_s(item), actual_duration):
        return True
    if _relative_change(planned_distance_m(item), activity.get("distance_m")):
        return True
    planned = planned_intensity(item)
    done = planned_intensity({"title": activity.get("name"), "extra": {}})
    explicit = activity.get("intensity")
    if isinstance(explicit, str) and explicit.strip().lower() in _INTENSITY:
        done = explicit.strip().lower()
    return bool(planned and done and planned != done)


def plan(
    engine: Engine,
    window: Window,
    today: date,
    *,
    include_calendar: bool = True,
    include_sync_status: bool = True,
) -> dict[str, Any]:
    with engine.connect() as conn:
        # One read reused for every day inside a bulk assessment snapshot.
        bound = getattr(engine, "conn", None)
        cache = bound.info.setdefault("vibesynergy_read_cache", {}) if bound is not None else None
        if cache is not None and "training_plans" in cache:
            plans = cache["training_plans"]
        else:
            plans = [dict(r._mapping) for r in conn.execute(select(training_plan))]
            if cache is not None:
                cache["training_plans"] = plans
        items = [
            dict(r._mapping)
            for r in conn.execute(
                select(planned_item)
                .where(
                    planned_item.c.date <= window.end,
                    or_(
                        planned_item.c.date >= window.start,
                        planned_item.c.extra["end"].as_string() > window.start.isoformat(),
                    ),
                )
                .order_by(planned_item.c.date, planned_item.c.kind)
            )
        ]
        upcoming = [
            dict(r._mapping)
            for r in conn.execute(
                select(planned_item)
                .where(planned_item.c.kind == "event", planned_item.c.date >= today)
                .order_by(planned_item.c.date)
            )
        ]
        done = conn.execute(
            select(
                activity_unified.c.local_date,
                activity_unified.c.sport,
                activity_unified.c.group_id,
                activity_unified.c.name,
                activity_unified.c.moving_s,
                activity_unified.c.elapsed_s,
                activity_unified.c.distance_m,
                activity_unified.c.ascent_m,
                activity_unified.c.kind,
            ).where(
                activity_unified.c.local_date.between(window.start, min(window.end, today)),
            )
        ).all()
    items = [i for i in items if i["date"] >= window.start or _overlaps_start(i, window.start)]
    if not include_calendar:
        items = [i for i in items if i["source"] != "caldav"]
    items = _combine_mirrors(items)
    from vibesynergy.query import calendar_mirror, icu_plan

    layer = icu_plan.plan_layer(engine, window.start, window.end)
    icu_races = icu_plan.races(engine, min(window.start, today))
    items = _merge_races(items, [r for r in icu_races if window.start <= r["date"] <= window.end])
    upcoming = _merge_races(upcoming, [r for r in icu_races if r["date"] >= today])
    items, not_in_plan = _merge_intervals(items, layer)
    # A training you give (coaching rules) is no hard session for you, whatever its title
    # says ("Ext. interval kort"): you run along easy.
    for item in items:
        if item.get("kind") in TRAINING_KINDS and coaching.is_coached_title(
            item.get("title"), item.get("sport")
        ):
            item["coached"] = True
            item["extra"] = {**(item.get("extra") or {}), "intensity": "easy"}
    ledger = calendar_mirror.ledger_index(engine)
    _attach_mirror_ledger(items, ledger)
    _attach_mirror_ledger(not_in_plan, ledger)
    by_day: dict[tuple[date, str | None], list] = {}
    # A training you give (kind coaching) counts only for a plan workout that is itself a
    # training you give (its title matches the coaching rules, such as "Groepstraining
    # geven, zelf Z1 60'"), of that sport and day, when nothing of your own matches.
    coached: dict[tuple[date, str | None], list] = {}
    for day, sport, group_id, name, moving_s, elapsed_s, distance_m, ascent_m, kind in done:
        bucket = coached if kind == "coaching" else by_day
        bucket.setdefault((day, family(sport)), []).append(
            {
                "id": group_id,
                "name": name,
                "moving_s": moving_s,
                "elapsed_s": elapsed_s,
                "distance_m": distance_m,
                "ascent_m": ascent_m,
            }
        )

    planned_counts = Counter(
        (i["date"], family(i["sport"])) for i in items if i["kind"] in TRAINING_KINDS
    )
    for item in not_in_plan:
        _common(item)
        item["status"] = "not_in_plan"
    for item in items:
        _common(item)
        day = date.fromisoformat(item["date"])
        if item["kind"] not in TRAINING_KINDS or day > today:
            item["status"] = "upcoming" if day >= today else "past"
            continue
        sport_family = family(item["sport"])
        matches = by_day.get((day, sport_family), []) if sport_family else []
        if (
            not matches
            and sport_family
            and coaching.is_coached_title(item.get("title"), item.get("sport"))
        ):
            matches = coached.get((day, sport_family), [])
        ambiguous = len(matches) > 1 or (matches and planned_counts[(day, sport_family)] > 1)
        item["completion_match"] = {
            "method": "day_sport_heuristic" if sport_family else "none",
            "confidence": "ambiguous" if ambiguous else "heuristic" if matches else "unverified",
            "candidates": matches,
        }
        if ambiguous or sport_family is None:
            item["status"] = "uncertain"
        elif matches:
            item["status"] = "changed" if departs_from_plan(item, matches[0]) else "done"
        else:
            item["status"] = "today" if day == today else "missed"
        item["done_by"] = matches[:1] if item["status"] in ("done", "changed") else []

    active = [
        _plan_row(p)
        for p in plans
        if (p["status"] or "").lower() not in ENDED
        and (p["start_date"] is None or p["start_date"] <= today)
        and (p["end_date"] is None or p["end_date"] >= today)
    ]
    planned_past = [
        i
        for i in items
        if i["kind"] in TRAINING_KINDS
        and (
            i["status"] in ("done", "changed", "missed")
            or (i["status"] == "uncertain" and i["date"] < str(today))
        )
    ]
    # The workout text sits in extra; pages read item["description"] (8-11: the calendar and
    # the week view showed no workout details for any planned session).
    for item in items:
        if not item.get("description"):
            text = (item.get("extra") or {}).get("description")
            if text:
                item["description"] = text
    saved = stored_priorities(engine)
    race_rows = [
        _event_row(event, today, saved)
        for event in upcoming
        if event["is_race"] or event["primary"]
    ]
    return {
        "active_plans": active,
        "calendar_sync": sync_status(engine, "caldav")
        if include_calendar and include_sync_status
        else None,
        "schedule_sync": _schedule_sync(engine) if include_sync_status else None,
        "calendar_mirror": calendar_mirror.summary(engine),
        "today": [i for i in items if i["date"] == str(today) and i["kind"] in TRAINING_KINDS],
        "commitments": [i for i in items if i["kind"] == "commitment"],
        "next_event": _event_row(upcoming[0], today, saved) if upcoming else None,
        "races": race_rows[:5],
        **race_phase_from_events(upcoming, today, saved),
        "items": items,
        "not_in_plan": not_in_plan,
        "plan_source": INTERVALS
        if layer["plan_start"] is not None and layer["plan_start"] <= window.end
        else "garmin",
        "plan_start": str(layer["plan_start"]) if layer["plan_start"] else None,
        "weeks": _weeks(engine, layer, window, today),
        "adherence": {
            "planned": len(planned_past),
            "done": sum(i["status"] == "done" for i in planned_past),
            "changed": sum(i["status"] == "changed" for i in planned_past),
            "missed": sum(i["status"] == "missed" for i in planned_past),
            "not_in_plan": sum(i["date"] <= str(today) for i in not_in_plan),
        },
    }


def _common(item: dict[str, Any]) -> None:
    item["completion_match"] = {"method": "none", "confidence": "unverified", "candidates": []}
    item["write_authority"] = False  # an ingested plan or date/sport match proves no ownership
    item["in_plan"] = item.get("in_plan", True)
    extra = item.get("extra") or {}
    if extra.get("ownership"):
        item["ownership"] = extra["ownership"]
    elif item["source"] == "garmin":
        item["ownership"] = "garmin"
    elif item["source"] == "caldav":
        item["ownership"] = "personal"
    else:
        item["ownership"] = "unknown"
    for key in ("raw_item_id", "updated_at", "subject_id"):
        item.pop(key, None)
    item["date"] = str(item["date"])


def _as_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _name_key(value: str | None) -> str:
    # Leading symbols go: VibeSynergy's own Garmin copy is "🏋️ Kracht AA 40'".
    text = re.sub(r"^\W+", "", value or "")
    return " ".join(text.casefold().split())


def _positive(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _icu_item(row: dict[str, Any]) -> dict[str, Any]:
    """An intervals.icu WORKOUT event as a calendar row."""
    from vibesynergy.query.icu_plan import version_token

    raw = row.get("raw") or {}
    sport_family = family(row.get("type")) or family(row.get("name"))
    extra: dict[str, Any] = {
        "icu_id": row["id"],
        "external_id": row.get("external_id"),
        "session_key": row.get("session_key"),
        "brick_part": row.get("brick_part"),
        "icu_type": row.get("type"),
        "description": row.get("description"),
        "tags": row.get("tags") or [],
        "version": version_token(row.get("updated"), raw),
        "ownership": "intervals_plan" if row.get("source") == "plan" else "intervals",
    }
    if duration := _positive(raw.get("moving_time")):
        extra["duration_s"] = duration
    if distance := _positive(raw.get("distance")):
        extra["distance_m"] = distance
    if load := _positive(raw.get("icu_training_load")):
        extra["load"] = load
    return {
        "source": INTERVALS,
        "source_id": row["id"],
        "date": row["date"],
        "kind": "workout",
        "title": row.get("name"),
        "sport": _SPORT.get(sport_family or "", row.get("type")),
        "plan_id": None,
        "is_race": False,
        "primary": False,
        "extra": extra,
        "plan_source": INTERVALS,
    }


def _plan_tags(rows: list[dict[str, Any]]) -> list[re.Pattern]:
    """External-id prefixes such as ``lake27`` (from ``lake27:2026-11-07:ride``)."""
    heads = set()
    for row in rows:
        head, sep, _rest = (row.get("external_id") or "").partition(":")
        if sep and head and not _DATE_HEAD.match(head):
            heads.add(head.casefold())
    return [re.compile(rf"\b{re.escape(head)}\b") for head in sorted(heads)]


def _copy_link(item: dict[str, Any], method: str) -> dict[str, Any]:
    return {
        "source": item["source"],
        "source_id": item["source_id"],
        "kind": item["kind"],
        "title": item.get("title"),
        "sport": item.get("sport"),
        "match": method,
        "mirrors": item.get("mirrors") or [],
    }


def _merge_intervals(items: list[dict], layer: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The intervals.icu workouts become the plan; Garmin copies fold into them.

    Returns the calendar rows and the Garmin workouts that are not in the plan.
    """
    rows = layer["workouts"]
    plan_start = layer["plan_start"]
    if not rows and plan_start is None:
        for item in items:
            if item["kind"] in TRAINING_KINDS:
                item.setdefault("plan_source", item["source"])
        return items, []
    from vibesynergy.query.icu_plan import link_duplicate

    workouts = [_icu_item(row) for row in rows]
    by_name: dict[tuple[date, str], list[dict]] = defaultdict(list)
    by_day: dict[date, list[dict]] = defaultdict(list)
    for workout in workouts:
        by_name[(workout["date"], _name_key(workout["title"]))].append(workout)
        by_day[workout["date"]].append(workout)
    tags = _plan_tags(rows)
    kept: list[dict] = []
    outside: list[dict] = []
    for item in items:
        if item["source"] != "garmin" or item["kind"] not in TRAINING_KINDS:
            kept.append(item)
            continue
        day = _as_day(item["date"])
        own = family(item.get("sport"))
        named = [
            w
            for w in by_name.get((day, _name_key(item.get("title"))), [])
            if not own or not family(w["sport"]) or family(w["sport"]) == own
        ]
        target, method = (named[0], "name") if named else (None, "")
        if target is None and " + " in (item.get("title") or ""):
            # VibeSynergy's brick: "🚴🏃 Lange rit + Brickloop" belongs to its ride.
            for part in (item.get("title") or "").split(" + "):
                found = by_name.get((day, _name_key(part)))
                if found:
                    target, method = found[0], "brick"
                    break
        title = (item.get("title") or "").casefold()
        if target is None and by_day.get(day) and any(tag.search(title) for tag in tags):
            same = [w for w in by_day[day] if own and family(w["sport"]) == own]
            target, method = (same or by_day[day])[0], "plan_tag"
        if target is not None:
            target.setdefault("garmin_copies", []).append(_copy_link(item, method))
            continue
        item["plan_source"] = "garmin"
        if plan_start is not None and day >= plan_start:
            item["in_plan"] = False
            outside.append(item)
        else:
            kept.append(item)
    # A Garmin Coach session on the same day and sport as one plan workout is the pair
    # ``resolve_duplicate`` decides on.
    coaches: dict[tuple, list[dict]] = defaultdict(list)
    plans: dict[tuple, list[dict]] = defaultdict(list)
    for item in outside:
        if item["kind"] == "plan_workout" and (sport := family(item.get("sport"))):
            coaches[(_as_day(item["date"]), sport)].append(item)
    for row in rows:
        if sport := family(row.get("type")) or family(row.get("name")):
            plans[(row["date"], sport)].append(row)
    for key, group in coaches.items():
        plan_rows = plans.get(key) or []
        if len(group) == 1 and len(plan_rows) == 1:
            link_duplicate(group[0], plan_rows[0], layer["marks"])
    merged = kept + workouts
    merged.sort(key=lambda i: (_as_day(i["date"]), i["kind"]))
    return merged, outside


def _merge_races(items: list[dict], icu_races: list[dict], *, add: bool = True) -> list[dict]:
    """Add intervals.icu races. One on the day of a Garmin race is that race, linked."""
    if not icu_races:
        return items
    garmin: dict[date, list[dict]] = defaultdict(list)
    for item in items:
        if item["kind"] == "event" and (item.get("is_race") or item.get("primary")):
            garmin[_as_day(item["date"])].append(item)
    added = []
    for race in icu_races:
        same = garmin.get(_as_day(race["date"])) or []
        if not same:
            # A copy: the window rows and the upcoming rows are formatted separately.
            added.append({**race, "extra": dict(race["extra"])})
            continue
        extra = race["extra"]
        same[0]["extra"] = {
            **(same[0].get("extra") or {}),
            "icu_priority": extra["icu_priority"],
            "intervals": {
                "id": extra["icu_id"],
                "category": extra["category"],
                "title": race.get("title"),
            },
        }
    if not added or not add:
        return items
    return sorted(items + added, key=lambda i: (_as_day(i["date"]), i["kind"]))


def _weeks(
    engine: Engine, layer: dict[str, Any], window: Window, today: date
) -> list[dict[str, Any]]:
    """Weekly NOTE text and TARGET time per sport, with what was done that week."""
    notes = {_as_day(row["date"]): row for row in layer["notes"]}
    targets: dict[date, list[dict]] = defaultdict(list)
    for row in layer["targets"]:
        targets[_as_day(row["date"])].append(row)
    mondays = sorted(
        day
        for day in set(notes) | set(targets)
        if day <= window.end and day + timedelta(days=6) >= window.start
    )
    if not mondays:
        return []
    first, last = mondays[0], min(mondays[-1] + timedelta(days=6), today)
    done: dict[tuple[date, str | None], dict[str, float]] = defaultdict(
        lambda: {"moving_s": 0.0, "distance_m": 0.0}
    )
    if first <= last:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    activity_unified.c.local_date,
                    activity_unified.c.sport,
                    activity_unified.c.moving_s,
                    activity_unified.c.elapsed_s,
                    activity_unified.c.distance_m,
                ).where(
                    activity_unified.c.local_date.between(first, last),
                    activity_unified.c.kind.is_distinct_from("coaching"),
                )
            ).all()
        for day, sport, moving_s, elapsed_s, distance_m in rows:
            bucket = done[(day - timedelta(days=day.weekday()), family(sport))]
            bucket["moving_s"] += float(moving_s or elapsed_s or 0)
            bucket["distance_m"] += float(distance_m or 0)
    out = []
    for monday in mondays:
        note = notes.get(monday)
        goals = []
        for row in targets.get(monday, []):
            sport_family = family(row.get("type"))
            got = done.get((monday, sport_family)) or {"moving_s": 0.0, "distance_m": 0.0}
            goal = row.get("targets") or {}
            goals.append(
                {
                    "id": row["id"],
                    "title": row.get("name"),
                    "sport": _SPORT.get(sport_family or "", row.get("type")),
                    "family": sport_family,
                    "time_s": _positive(goal.get("time_target")),
                    "distance_m": _positive(goal.get("distance_target")),
                    "load": _positive(goal.get("load_target")),
                    "done_s": round(got["moving_s"], 1),
                    "done_distance_m": round(got["distance_m"], 1),
                }
            )
        times = [g["time_s"] for g in goals if g["time_s"]]
        out.append(
            {
                "start": str(monday),
                "end": str(monday + timedelta(days=6)),
                "note": (
                    {"id": note["id"], "title": note.get("name"), "text": note.get("description")}
                )
                if note
                else None,
                "targets": goals,
                "time_s": round(sum(times), 1) if times else None,
            }
        )
    return out


def _schedule_sync(engine) -> dict[str, Any]:
    status = sync_status(engine, "garmin")
    datasets = [row for row in status["datasets"] if row["dataset"] == "scheduled_workouts"]
    return {
        "datasets": datasets,
        "problems": [row for row in datasets if row["problem_stages"]],
    }


def _attach_mirror_ledger(items: list[dict], ledger: dict[str, dict]) -> None:
    """Show the owned-mirror revision. This is not write permission."""
    for item in items:
        extra = item.get("extra") or {}
        key = extra.get("native_calendar_key") or extra.get("mirror_key")
        row = ledger.get(key) if key else None
        if not row:
            continue
        item["mirror_state"] = row["state"]
        item["mirror_revision"] = row["revision"]
        for link in item.get("mirrors") or []:
            link["revision"] = row["revision"]
            link["ownership"] = row["state"]


def _plan_row(p: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in p.items() if k not in ("raw_item_id", "updated_at", "subject_id")}
    for key in ("start_date", "end_date"):
        out[key] = str(out[key]) if out[key] else None
    return out


def _priority_of(
    event: dict[str, Any], saved: dict[tuple[str, str], str]
) -> tuple[str | None, str]:
    source = event.get("source")
    source_id = event.get("source_id")
    stored = saved.get((source, source_id)) if source and source_id else None
    letter, origin = effective_priority(bool(event.get("primary")), stored)
    if letter is None:
        # The intervals.icu race category (RACE_A/B/C) when nothing else sets it.
        icu = (event.get("extra") or {}).get("icu_priority")
        if icu in ("A", "B", "C"):
            return icu, "intervals"
    return letter, origin


def _event_row(
    e: dict[str, Any], today: date, saved: dict[tuple[str, str], str] | None = None
) -> dict[str, Any]:
    priority, origin = _priority_of(e, saved or {})
    return {
        "date": str(e["date"]),
        "days_away": (e["date"] - today).days,
        "title": e["title"],
        "is_race": e["is_race"],
        "primary": bool(e.get("primary")),
        "source": e.get("source"),
        "source_id": e.get("source_id"),
        "priority": priority,
        "priority_origin": origin,
        "location": (e["extra"] or {}).get("location"),
        "target": (e["extra"] or {}).get("completionTarget"),
        "intervals_id": (e["extra"] or {}).get("icu_id")
        or ((e["extra"] or {}).get("intervals") or {}).get("id"),
    }


def _event_day(event: dict[str, Any]) -> date:
    value = event["date"]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def race_phase_from_events(
    events: list[dict[str, Any]],
    today: date,
    saved: dict[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """The next A race sets the phase. An earlier race is a tune-up, not the taper.

    A is the stored race priority when one exists. Otherwise a Garmin primary event is A.
    With no A race, Garmin's primary event still leads, then the soonest race.
    """
    saved = saved or {}
    races = [event for event in events if event.get("is_race") or event.get("primary")]
    races.sort(key=_event_day)
    ranked: list[tuple[dict[str, Any], str | None, str]] = []
    for event in races:
        letter, origin = _priority_of(event, saved)
        ranked.append((event, letter, origin))
    chosen = next((item for item in ranked if item[1] == "A"), None)
    if chosen is None:
        chosen = next((item for item in ranked if item[0].get("primary")), None)
    if chosen is None and ranked:
        chosen = ranked[0]
    if chosen is None:
        return {"next_race": None, "plan_phase": None}
    race, priority, origin = chosen
    race_day = _event_day(race)
    days = (race_day - today).days
    supporting = [item for item in ranked if item[0] is not race and _event_day(item[0]) < race_day]
    extra = race.get("extra") or {}
    target = extra.get("completionTarget") or {}
    distance = None
    if isinstance(target, dict) and target.get("unitType") == "distance":
        distance = target.get("value")
    # Garmin completion targets use kilometres; the raw calendar distance uses metres.
    distance_m = distance * 1000 if isinstance(distance, (int, float)) else extra.get("distance")
    return {
        "next_race": {
            "name": race["title"],
            "date": race_day.isoformat(),
            "days_to_go": days,
            "distance_m": distance_m,
            "source": race.get("source"),
            "source_id": race.get("source_id"),
            "priority": priority,
            "priority_origin": origin,
            "is_primary": bool(race.get("primary")),
        },
        "plan_phase": {
            "phase": "taper" if days <= TAPER_DAYS else "build",
            "days_left": days if days <= TAPER_DAYS else days - TAPER_DAYS,
        },
        "supporting": [
            {
                "name": event.get("title") or "",
                "date": _event_day(event).isoformat(),
                "days_to_go": (_event_day(event) - today).days,
                "source": event.get("source"),
                "source_id": event.get("source_id"),
                "priority": letter,
                "priority_origin": event_origin,
                "is_primary": bool(event.get("primary")),
            }
            for event, letter, event_origin in supporting
        ],
    }


def calendar_races(engine: Engine, today: date) -> list[dict[str, Any]]:
    """Upcoming races on the plan calendar, whether or not a coach event source is on.

    Priority is VibeSynergy's own A/B/C choice when one is stored. Otherwise a Garmin
    primary race is A, and every other race is unset.
    """
    saved = stored_priorities(engine)
    with engine.connect() as conn:
        events = [
            dict(row._mapping)
            for row in conn.execute(
                select(planned_item)
                .where(
                    planned_item.c.kind == "event",
                    planned_item.c.date >= today,
                    or_(planned_item.c.is_race, planned_item.c.primary),
                )
                .order_by(planned_item.c.date)
            )
        ]
    from vibesynergy.query import icu_plan

    # An intervals.icu race links to its Garmin twin (its category fills an unset priority).
    # One only in intervals.icu is listed too, but only Garmin races can store a priority.
    events = _merge_races(events, icu_plan.races(engine, today))
    races = []
    for event in events:
        row = _event_row(event, today, saved)
        row["name"] = event["title"]
        row["editable"] = event.get("source") != INTERVALS
        races.append(row)
    return races


def race_phase(engine: Engine, today: date) -> dict[str, Any]:
    """Cheap shared projection for daily reads that do not need a full calendar."""
    with engine.connect() as conn:
        events = [
            dict(row._mapping)
            for row in conn.execute(
                select(planned_item)
                .where(
                    planned_item.c.kind == "event",
                    planned_item.c.date >= today,
                    or_(planned_item.c.is_race, planned_item.c.primary),
                )
                .order_by(planned_item.c.date)
            )
        ]
    from vibesynergy.query import icu_plan

    events = _merge_races(events, icu_plan.races(engine, today))
    return race_phase_from_events(events, today, stored_priorities(engine))


def _mirror_title_key(item: dict) -> tuple[date, str] | None:
    title = (item.get("title") or "").strip().casefold()
    if not title:
        return None
    day = item["date"]
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return day, title


def _mirror_link(mirror: dict, garmin: dict) -> dict:
    extra = mirror.get("extra") or {}
    return {
        "source": "caldav",
        "source_id": mirror["source_id"],
        "uid": extra.get("uid"),
        "href": extra.get("href"),
        "etag": extra.get("etag"),
        "snapshot_at": extra.get("snapshot_at"),
        "schedule_matches": mirror["date"] == garmin["date"],
        "write_authority": False,
    }


def _combine_mirrors(items: list[dict]) -> list[dict]:
    """Hide calendar mirrors that match a unique Garmin row (native id or title+date)."""
    by_native_key: dict[str, list[dict]] = {}
    by_title_date: dict[tuple[date, str], list[dict]] = {}
    for item in items:
        if item["source"] != "garmin":
            continue
        extra = item.get("extra") or {}
        if key := extra.get("native_calendar_key"):
            by_native_key.setdefault(key, []).append(item)
        if title_key := _mirror_title_key(item):
            by_title_date.setdefault(title_key, []).append(item)
    combined = []
    for item in items:
        extra = item.get("extra") or {}
        if not (
            item["source"] == "caldav"
            and item["kind"] == "mirror"
            and extra.get("ownership") == "verified_legacy_mirror"
        ):
            combined.append(item)
            continue
        matches = by_native_key.get(extra.get("mirror_key") or "", [])
        if len(matches) != 1 and (title_key := _mirror_title_key(item)):
            matches = by_title_date.get(title_key, [])
        if len(matches) == 1:
            matches[0].setdefault("mirrors", []).append(_mirror_link(item, matches[0]))
        else:
            combined.append(item)
    return combined


def _overlaps_start(item: dict, day: date) -> bool:
    """End-exclusive calendar spans; ordinary training rows never imply an interval.

    A clock time such as ``20:00`` is when a commitment starts or ends that day. It does
    not reach into a later window, and it is not training load.
    """
    extra = item.get("extra") or {}
    end = extra.get("end")
    if not isinstance(end, str) or not end:
        return False
    if len(end) == 10:
        return date.fromisoformat(end) > day
    if "T" not in end and "-" not in end:
        return False
    zone = ZoneInfo(extra.get("timezone", "UTC"))
    return datetime.fromisoformat(end) > datetime.combine(day, time.min, zone)


def planned_moving_s(engine: Any, window: Window) -> float | None:
    """Planned workout time in the window from the intervals.icu plan, or None without a plan.

    The safety floor compares the last 7 days with this: a build week that follows
    the plan is no load spike, even after a quiet block."""
    from sqlalchemy import func

    from vibesynergy.store.schema import icu_event

    moving = func.json_extract(icu_event.c.raw, "$.moving_time")
    with engine.connect() as conn:
        rows = conn.execute(
            select(moving).where(
                icu_event.c.category == "WORKOUT",
                icu_event.c.date.between(window.start, window.end),
            )
        ).all()
    total = sum(float(r[0]) for r in rows if isinstance(r[0], (int, float)) and r[0] > 0)
    return total or None
