"""The training calendar: coaching plans, planned workouts and events (races), and how the
planned sessions compare with what was done. Used by the MCP tool vibesynergy_plan and the
portal's Dashboard.

The plan is VibeSynergy's own model (``plan_model.plan_layer``), and only that (#111):
intervals.icu holds a one-way copy and is never read as the plan. Its events (workouts, week
notes, targets, races) are not planned sessions, and a change made there changes nothing
here. From the first plan workout on, a Garmin scheduled workout that is a copy of a plan
workout (same day, sport family and name, or a title carrying the plan's tag such as
``(race27)``) is folded into that workout as ``garmin_copies``. Any other Garmin workout
from that day on (Garmin Coach's "Basis", "Drempel", one added by hand in Garmin) is not
VibeSynergy's and is not shown (#153): it is in no list of this result, not counted, and
not passed on to the pages or the AI coach. Its synced row stays in the database. Earlier
days, and an install without a plan, keep the Garmin calendar as the plan.

Races come from VibeSynergy's own race list (``races``). A Garmin event is a copy at most:
one on the day of a race folds into that race (``extra.garmin``); any other is not shown
either."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, or_, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import coaching, multisport, plan_match
from vibesynergy.query.safety_floor import title_band
from vibesynergy.query.views import sync_status
from vibesynergy.store.schema import activity_unified, planned_item, training_plan

INTERVALS = "intervals_icu"
VIBESYNERGY = "vibesynergy"
# ``source`` of a calendar row that is a plan workout: VibeSynergy's own plan only (#111).
PLAN_SOURCES = (VIBESYNERGY,)

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


# Deviation from the plan (#88). Duration alone is not a deviation: a longer session at a
# low effort is the session that was planned, and a session of the planned length that ran
# above its zone is not.
LOW_RPE = 4  # 1-10: up to "comfortable" a longer session is no deviation
ABOVE_MIN_S = 600  # time above the planned zone ceiling before a session counts as harder
ABOVE_SHARE = 0.15  # … and its share of the session
LOW_ZONE = 2  # without an RPE: nearly all heart-rate time in zones 1-2 is a low effort
LOW_ZONE_SHARE = 0.9
_ZONE = re.compile(r"\bz(?:one)?\s?([1-7])\b", re.IGNORECASE)
_ICU_NAME = re.compile(r"intervals[ ._-]?icu", re.IGNORECASE)


def zone_ceiling(item: dict[str, Any]) -> int | None:
    """The highest heart-rate zone the planned session asks for, when the plan says so:
    the highest "Z<n>" in its title, else zone 1 for a training you give (you run along
    easy), else zone 2 for a session planned easy. ``None``: no ceiling to judge by."""
    found = [int(z) for z in _ZONE.findall(str(item.get("title") or ""))]
    if found:
        return max(found)
    if item.get("coached"):
        return 1
    if planned_intensity(item) == "easy":
        return 2
    return None


def _zone_seconds(activity: dict[str, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for zone, seconds in (activity.get("hr_zones") or {}).items():
        try:
            out[int(zone)] = float(seconds)
        except (TypeError, ValueError):
            continue
    return out


def seconds_above(item: dict[str, Any], activity: dict[str, Any]) -> float | None:
    """Heart-rate time above the planned zone ceiling; ``None`` without a ceiling or zones."""
    ceiling = zone_ceiling(item)
    zones = _zone_seconds(activity)
    if ceiling is None or not zones:
        return None
    return sum(seconds for zone, seconds in zones.items() if zone > ceiling)


def _low_effort(activity: dict[str, Any]) -> bool | None:
    """Whether the session was a low effort: by RPE when there is one, else by heart-rate
    zones. ``None`` when neither is known."""
    rpe = activity.get("rpe")
    if isinstance(rpe, (int, float)) and not isinstance(rpe, bool):
        return rpe <= LOW_RPE
    zones = _zone_seconds(activity)
    total = sum(zones.values())
    if total <= 0:
        return None
    low = sum(seconds for zone, seconds in zones.items() if zone <= LOW_ZONE)
    return low / total >= LOW_ZONE_SHARE


def deviation(
    item: dict[str, Any], activity: dict[str, Any], structure: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """How the matched activity departs from the planned session, or ``None`` when it is
    that session. ``kind`` is one of:

    - ``harder``: time above the planned zone ceiling (10 minutes and 15% of the session,
      or more), whatever its length;
    - ``longer``: over 25% longer and not at a low effort (RPE above 4, or heart rate not
      nearly all in zones 1-2), or longer with nothing known about the effort;
    - ``shorter``: over 25% shorter;
    - ``distance``: the distance is off by more than 25%;
    - ``structure``: the laps clearly show another workout than the planned steps
      (``plan_match.compare`` gave ``differs``, #116): 3x300 planned, 4x50 + 3x100 done, or
      a tempo block in an easy run. ``planned`` and ``done`` say which;
    - ``intensity``: both sides name an intensity and the names differ.

    A missing number is not a difference. Longer at a low effort is not a deviation, but a
    different workout of the same size or longer is.
    """
    actual = activity.get("moving_s") or activity.get("elapsed_s")
    planned = planned_duration_s(item)
    above = seconds_above(item, activity)
    if above is not None and above >= ABOVE_MIN_S and above >= (actual or 0) * ABOVE_SHARE:
        return {"kind": "harder", "above_s": round(above), "ceiling": zone_ceiling(item)}
    if _relative_change(planned, actual):
        assert planned is not None and actual is not None
        if float(actual) < float(planned):
            return {"kind": "shorter", "planned_s": planned, "actual_s": actual}
        if _low_effort(activity) is not True:
            return {"kind": "longer", "planned_s": planned, "actual_s": actual}
        # Longer, at a low effort: the distance is longer too, and that is no deviation.
    elif _relative_change(planned_distance_m(item), activity.get("distance_m")):
        return {
            "kind": "distance",
            "planned_m": planned_distance_m(item),
            "actual_m": activity.get("distance_m"),
        }
    if structure and structure.get("check") == "differs":
        return {
            "kind": "structure",
            "planned": structure.get("planned"),
            "done": structure.get("done"),
            "done_shape": structure.get("done_shape"),
        }
    wanted = planned_intensity(item)
    # "Zwift - Intervals icu: Rustige rit Z2 1u" names where the workout came from, not
    # intervals: the word is no intensity (#116, 19-11).
    name = _ICU_NAME.sub(" ", str(activity.get("name") or ""))
    done = planned_intensity({"title": name, "extra": {}})
    explicit = activity.get("intensity")
    if isinstance(explicit, str) and explicit.strip().lower() in _INTENSITY:
        done = explicit.strip().lower()
    if wanted and done and wanted != done:
        return {"kind": "intensity", "planned": wanted, "done": done}
    return None


def departs_from_plan(item: dict[str, Any], activity: dict[str, Any]) -> bool:
    """True when the matched activity is not the session that was planned (``deviation``)."""
    return deviation(item, activity) is not None


def _effort_of(engine: Engine, groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{activity id: {rpe, hr_zones}}`` for the day's activities (``id``, ``sources``):
    what the deviation is judged on. Zones are the stored ones (``zone_times``); a session
    whose zones are not computed yet is judged without them."""
    from vibesynergy.query import activity_effort
    from vibesynergy.store.schema import activity_zone_own

    if not groups:
        return {}
    try:
        effort = activity_effort.for_groups(
            engine, None, [{"group_id": g["id"], "sources": g["sources"]} for g in groups]
        )
        with engine.connect() as conn:
            zones = {
                row.group_id: row.zones
                for row in conn.execute(
                    select(activity_zone_own.c.group_id, activity_zone_own.c.zones).where(
                        activity_zone_own.c.kind == "hr",
                        activity_zone_own.c.group_id.in_([g["id"] for g in groups]),
                    )
                )
            }
    except Exception:  # the plan still answers; the deviation falls back to duration
        return {}
    return {
        group["id"]: {
            "rpe": ((effort.get(group["id"]) or {}).get("rpe") or {}).get("value"),
            "hr_zones": zones.get(group["id"]) or {},
        }
        for group in groups
    }


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
                    # RunSynergy group trainings are proposals for a plan session, not plan.
                    planned_item.c.kind != "group_training",
                    planned_item.c.date <= window.end,
                    or_(
                        planned_item.c.date >= window.start,
                        planned_item.c.extra["end"].as_string() > window.start.isoformat(),
                    ),
                )
                .order_by(planned_item.c.date, planned_item.c.kind)
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
                activity_unified.c.sources,
            ).where(
                activity_unified.c.local_date.between(window.start, min(window.end, today)),
            )
        ).all()
    items = [i for i in items if i["date"] >= window.start or _overlaps_start(i, window.start)]
    if not include_calendar:
        items = [i for i in items if i["source"] != "caldav"]
    items = _combine_mirrors(items)
    from vibesynergy.query import calendar_mirror, plan_model, races

    own = plan_model.leading_plan(engine)
    layer = plan_model.plan_layer(engine, own, window.start, window.end) if own else NO_PLAN
    own_races = races.rows(engine, today)
    upcoming = [r for r in own_races if r["date"] >= today]
    # Garmin workouts and events that are not VibeSynergy's own are left out (#153): the
    # plan is VibeSynergy's, and what Garmin Coach or the athlete put on the Garmin
    # calendar is no part of it. Only VibeSynergy's own copies fold in (garmin_copies).
    items, _other_events = _fold_events(
        items,
        [r for r in own_races if window.start <= r["date"] <= window.end],
        races.event_rows(engine, window.start, window.end),
    )
    items, _other_workouts = _merge_plan(items, layer, _model_item)
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
    by_day: dict[tuple[date, str | None], list] = {}
    # A training you give (kind coaching) counts only for a plan workout that is itself a
    # training you give (its title matches the coaching rules, such as "Groepstraining
    # geven, zelf Z1 60'"), of that sport and day, when nothing of your own matches.
    coached: dict[tuple[date, str | None], list] = {}
    matched: list[dict[str, Any]] = []
    for row in done:
        day, sport, group_id, name, moving_s, elapsed_s, distance_m, ascent_m, kind, refs = row
        bucket = coached if kind == "coaching" else by_day
        found = {
            "id": group_id,
            "sport": sport,
            "name": name,
            "moving_s": moving_s,
            "elapsed_s": elapsed_s,
            "distance_m": distance_m,
            "ascent_m": ascent_m,
        }
        matched.append({"id": group_id, "sources": refs or []})
        bucket.setdefault((day, family(sport)), []).append(found)
    effort = _effort_of(engine, matched)
    laps = _laps_of(engine, items, done, today)

    planned_counts = Counter(
        (i["date"], family(i["sport"])) for i in items if i["kind"] in TRAINING_KINDS
    )
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
            found = {**matches[0], **effort.get(matches[0]["id"], {})}
            compared = plan_match.compare(
                item.get("_steps"),
                sport_family,
                laps.get(matches[0]["id"]),
                coached=bool(item.get("coached")),
            )
            differs = deviation(item, found, compared)
            item["status"] = "changed" if differs else "done"
            if differs:
                item["deviation"] = differs
            item["comparison"] = {**compared, "rpe": found.get("rpe")}
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
        steps = item.pop("_steps", None)
        if item["kind"] in TRAINING_KINDS and "comparison" not in item:
            planned = plan_match.planned_text(steps, family(item.get("sport")))
            item["comparison"] = {"check": None, "planned": planned, "done": None}
        if not item.get("description"):
            text = (item.get("extra") or {}).get("description")
            if text:
                item["description"] = text
    race_rows = [_event_row(event, today) for event in upcoming]
    return {
        "active_plans": active,
        "calendar_sync": sync_status(engine, "caldav")
        if include_calendar and include_sync_status
        else None,
        "schedule_sync": _schedule_sync(engine) if include_sync_status else None,
        "calendar_mirror": calendar_mirror.summary(engine),
        "today": [i for i in items if i["date"] == str(today) and i["kind"] in TRAINING_KINDS],
        "commitments": [i for i in items if i["kind"] == "commitment"],
        "next_event": race_rows[0] if race_rows else None,
        "races": race_rows[:5],
        **race_phase_from_events(upcoming, today),
        "items": items,
        "plan_source": VIBESYNERGY
        if layer["plan_start"] is not None and layer["plan_start"] <= window.end
        else "garmin",
        "plan_start": str(layer["plan_start"]) if layer["plan_start"] else None,
        "weeks": _weeks(engine, layer, window, today),
        "adherence": {
            "planned": len(planned_past),
            "done": sum(i["status"] == "done" for i in planned_past),
            "changed": sum(i["status"] == "changed" for i in planned_past),
            "missed": sum(i["status"] == "missed" for i in planned_past),
        },
    }


def _laps_of(
    engine: Engine, items: list[dict[str, Any]], done: Any, today: date
) -> dict[str, list[dict]]:
    """Laps of the activities a planned session with steps may be matched to (same day and
    sport family, swim, run or ride): what the structure check reads (#116)."""
    wanted = {
        (_as_day(i["date"]), family(i.get("sport")))
        for i in items
        if i["kind"] in TRAINING_KINDS and i.get("_steps") and _as_day(i["date"]) <= today
    }
    groups = [
        {"id": row[2], "sources": row[9] or []}
        for row in done
        if (row[0], family(row[1])) in wanted and family(row[1]) in plan_match.CHECKED_FAMILIES
    ]
    if not groups:
        return {}
    try:
        return plan_match.laps_for(engine, groups)
    except Exception:  # the plan still answers; the sessions are then not checked
        return {}


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


def _model_item(row: dict[str, Any]) -> dict[str, Any]:
    """A session of VibeSynergy's own plan as a calendar row."""
    sport_family = family(row.get("type")) or family(row.get("name"))
    extra: dict[str, Any] = {
        "brick_part": row["slot"] if row.get("slot") in ("fiets", "loop") else None,
        "multi_part": multisport.slot_part(row.get("slot")),
        "icu_type": row.get("type"),
        "description": row.get("description"),
        "ownership": "vibesynergy_plan",
    }
    if row.get("icu_id"):
        # Garmin delivery and the activity pairing are keyed by the intervals.icu copy.
        extra["icu_id"] = row["icu_id"]
    if duration := _positive(row.get("duration_s")):
        extra["duration_s"] = duration
    if distance := _positive(row.get("distance_m")):
        extra["distance_m"] = distance
    return {
        "_steps": row.get("steps") or [],
        "source": VIBESYNERGY,
        "source_id": row["id"],
        "session_id": row["id"],
        "date": row["date"],
        "kind": "workout",
        "title": row.get("name"),
        "sport": _SPORT.get(sport_family or "", row.get("type")),
        "plan_id": None,
        "is_race": False,
        "primary": False,
        "extra": extra,
        "plan_source": VIBESYNERGY,
    }


def _plan_tags(rows: list[dict[str, Any]]) -> list[re.Pattern]:
    """External-id prefixes such as ``race27`` (from ``race27:2026-11-07:ride``)."""
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


def _merge_plan(
    items: list[dict], layer: dict[str, Any], as_item: Callable[[dict[str, Any]], dict[str, Any]]
) -> tuple[list[dict], list[dict]]:
    """The layer's workouts become the plan; Garmin copies fold into them.

    Returns the calendar rows and the Garmin workouts that are not in the plan (marked
    ``in_plan`` false). ``plan`` does not show those (#153); they are returned for tests.
    """
    rows = layer["workouts"]
    plan_start = layer["plan_start"]
    if not rows and plan_start is None:
        for item in items:
            if item["kind"] in TRAINING_KINDS:
                item.setdefault("plan_source", item["source"])
        return items, []
    workouts = [as_item(row) for row in rows]
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
    merged = kept + workouts
    merged.sort(key=lambda i: (_as_day(i["date"]), i["kind"]))
    return merged, outside


NO_PLAN: dict[str, Any] = {"workouts": [], "notes": [], "targets": [], "plan_start": None}


def _fold_events(
    items: list[dict], own_races: list[dict], own_events: list[dict] | None = None
) -> tuple[list[dict], list[dict]]:
    """VibeSynergy's races and events join the calendar; Garmin events are copies at most.

    A Garmin event linked to a race or event (``links``, the dedupe of #91), or on the day
    of a race, folds into it as ``extra.garmin``. Any other Garmin event is not in the plan
    and never read as a race. Returns the calendar rows and those other events, which
    ``plan`` does not show (#153).
    """
    own_events = own_events or []
    by_link = {
        str((r["extra"].get("links") or {}).get("garmin")): r
        for r in [*own_races, *own_events]
        if (r["extra"].get("links") or {}).get("garmin")
    }
    by_day: dict[date, list[dict]] = defaultdict(list)
    for r in own_races:
        by_day[_as_day(r["date"])].append(r)
    kept: list[dict] = []
    outside: list[dict] = []
    for item in items:
        if item["kind"] != "event" or item["source"] == VIBESYNERGY:
            kept.append(item)
            continue
        target = by_link.get(str(item["source_id"])) or next(
            iter(by_day.get(_as_day(item["date"])) or []), None
        )
        if target is not None:
            target["extra"]["garmin"] = {"source_id": item["source_id"], "title": item.get("title")}
            continue
        item["in_plan"] = False
        item["plan_source"] = item["source"]
        outside.append(item)
    added = [{**r, "extra": dict(r["extra"])} for r in [*own_races, *own_events]]
    return sorted(kept + added, key=lambda i: (_as_day(i["date"]), i["kind"])), outside


def _weeks(
    engine: Engine, layer: dict[str, Any], window: Window, today: date
) -> list[dict[str, Any]]:
    """The week's note and target time per sport, with what was done that week."""
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


def _priority_of(event: dict[str, Any]) -> tuple[str | None, str]:
    """VibeSynergy's own A/B/C letter: ``saved`` when set, else ``unset`` (#111)."""
    letter = event.get("priority")
    if letter in ("A", "B", "C"):
        return letter, "saved"
    return None, "unset"


def _event_row(e: dict[str, Any], today: date) -> dict[str, Any]:
    priority, origin = _priority_of(e)
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
        "sport": e.get("sport"),
        "race_id": e.get("race_id"),
        # The id of the race's copy in intervals.icu, for a link; never read back.
        "intervals_id": (e["extra"] or {}).get("icu_id"),
    }


def _event_day(event: dict[str, Any]) -> date:
    value = event["date"]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def race_phase_from_events(events: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """The next A race sets the phase. An earlier race is a tune-up, not the taper.

    ``events`` are VibeSynergy's races (``races.rows``); the priority is their own letter.
    The primary race is the next A race (#120). With no A race the soonest race leads.
    """
    races = [event for event in events if event.get("is_race") or event.get("primary")]
    races.sort(key=_event_day)
    ranked: list[tuple[dict[str, Any], str | None, str]] = []
    for event in races:
        letter, origin = _priority_of(event)
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
    """Upcoming races: VibeSynergy's own race list, with its A/B/C priority (#111)."""
    from vibesynergy.query import races

    out = []
    for event in races.rows(engine, today, since=today):
        row = _event_row(event, today)
        row["name"] = event["title"]
        row["editable"] = True
        out.append(row)
    return out


def race_phase(engine: Engine, today: date) -> dict[str, Any]:
    """Cheap shared projection for daily reads that do not need a full calendar."""
    from vibesynergy.query import races

    return race_phase_from_events(races.rows(engine, today, since=today), today)


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
    """Planned workout time in the window from VibeSynergy's plan, or None without a plan.

    The safety floor compares the last 7 days with this: a build week that follows
    the plan is no load spike, even after a quiet block."""
    from vibesynergy.query import plan_model

    if own := plan_model.leading_plan(engine):
        return plan_model.planned_seconds(engine, own, window.start, window.end) or None
    return None
