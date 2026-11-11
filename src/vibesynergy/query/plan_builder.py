"""The plan builder's settings: what a new plan is built for and how.

choose the goal from the races you entered or a new one, the sport (running or
triathlon, every distance or a custom one, and its type), and the options. Until you change
them, the settings follow the current plan. Stored in the portal prefs (``plan_builder``);
nothing is built or written to the plan here.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

METHODS = ("friel", "polarised", "pyramidal")
SPORTS = {
    "run": {
        "distances": ("5k", "10k", "half", "marathon", "ultra", "custom"),
        "types": ("road", "trail"),
    },
    "triathlon": {
        "distances": ("super_sprint", "sprint", "olympic", "middle", "full", "custom"),
        "types": ("road", "cross", "offroad"),
    },
}
RHYTHMS = ("3:1", "2:1")
PRIORITIES = ("A", "B", "C")
_FULL = re.compile(r"lakeside|ironman|full|hele afstand|226", re.IGNORECASE)
_MIDDLE = re.compile(r"70\.3|half|middle|halve|xxl", re.IGNORECASE)
_HOURS = re.compile(r"~(\d+(?:[.,]\d+)?)u")


def _guess_sport(title: str) -> dict[str, Any]:
    low = title.lower()
    if "tri" in low or _FULL.search(title):
        distance = (
            "full" if _FULL.search(title) else "middle" if _MIDDLE.search(title) else "olympic"
        )
        return {
            "sport": "triathlon",
            "distance": distance,
            "type": "cross" if "cross" in low else "road",
        }
    trail = "trail" in low
    return {
        "sport": "run",
        "distance": "ultra" if trail else "marathon",
        "type": "trail" if trail else "road",
    }


def defaults(engine: Any, today: date) -> dict[str, Any]:
    """The current plan, read as builder settings."""
    from vibesynergy.query import plan_model, training_plan

    overview = training_plan.overview(engine, today)
    races = overview["races"]
    goal = next((r for r in races if r.get("priority") == "A"), races[-1] if races else None)
    loading = [
        float(m.group(1).replace(",", "."))
        for w in overview["weeks"]
        if "laadweek" in (w.get("title") or "")
        for m in [_HOURS.search(w.get("title") or "")]
        if m
    ]
    model = plan_model.summary(engine) or {}
    out: dict[str, Any] = {
        "goal": {"race": goal["source_id"] if goal else None},
        "method": model.get("method") if model.get("method") in METHODS else "friel",
        "hours_min": min(loading) if loading else 6.0,
        "hours_max": max(loading) if loading else 10.0,
        "rhythm": "3:1",
        "doubles": True,
        "strength": True,
        "tests": True,
        "taper_weeks": 2,
        # When you train: part of building a plan, editable while it runs.
        "rest_days": [],
        "fixed": [],
    }
    out.update(
        _guess_sport(goal["title"])
        if goal
        else {"sport": "triathlon", "distance": "full", "type": "road"}
    )
    return out


def validate(value: Any) -> dict[str, Any]:
    """The stored settings, checked; unknown keys or values are refused."""
    if not isinstance(value, dict):
        raise ValueError("plan_builder: an object")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "goal":
            if not isinstance(item, dict):
                raise ValueError("plan_builder.goal: an object")
            goal: dict[str, Any] = {}
            if item.get("race"):
                goal["race"] = str(item["race"])[:200]
            new = item.get("new")
            if new:
                if not isinstance(new, dict) or not str(new.get("name") or "").strip():
                    raise ValueError("plan_builder.goal.new: a name and a date")
                day = date.fromisoformat(str(new.get("date")))
                priority = new.get("priority") or "A"
                if priority not in PRIORITIES:
                    raise ValueError("plan_builder.goal.new.priority: A, B or C")
                goal["new"] = {
                    "name": str(new["name"]).strip()[:120],
                    "date": day.isoformat(),
                    "priority": priority,
                }
            out["goal"] = goal
        elif key == "sport":
            if item not in SPORTS:
                raise ValueError("plan_builder.sport: run or triathlon")
            out[key] = item
        elif key in ("distance", "type"):
            out[key] = str(item)
        elif key == "custom":
            if not isinstance(item, dict):
                raise ValueError("plan_builder.custom: distances in km")
            out[key] = {
                k: float(v) for k, v in item.items() if k in ("swim_km", "bike_km", "run_km") and v
            }
        elif key == "method":
            if item not in METHODS:
                raise ValueError(f"plan_builder.method: one of {', '.join(METHODS)}")
            out[key] = item
        elif key in ("hours_min", "hours_max"):
            hours = float(item)
            if not 1 <= hours <= 40:
                raise ValueError(f"plan_builder.{key}: 1 to 40 hours")
            out[key] = hours
        elif key == "rhythm":
            if item not in RHYTHMS:
                raise ValueError("plan_builder.rhythm: 3:1 or 2:1")
            out[key] = item
        elif key in ("doubles", "strength", "tests"):
            out[key] = bool(item)
        elif key == "rest_days":
            if not isinstance(item, list) or any(
                isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in item
            ):
                raise ValueError("plan_builder.rest_days: weekdays 0 (Monday) to 6 (Sunday)")
            if len(set(item)) > 5:
                raise ValueError("plan_builder.rest_days: at most five")
            out[key] = sorted(set(item))
        elif key == "fixed":
            out[key] = _fixed(item)
        elif key == "taper_weeks":
            weeks = int(item)
            if not 1 <= weeks <= 4:
                raise ValueError("plan_builder.taper_weeks: 1 to 4")
            out[key] = weeks
        else:
            raise ValueError(f"plan_builder: unknown setting {key!r}")
    sport = out.get("sport")
    if sport and "distance" in out and out["distance"] not in SPORTS[sport]["distances"]:
        raise ValueError("plan_builder.distance does not fit the sport")
    if sport and "type" in out and out["type"] not in SPORTS[sport]["types"]:
        raise ValueError("plan_builder.type does not fit the sport")
    return out


FIXED_SPORTS = ("run", "ride", "swim", "strength")
MAX_FIXED = 10


def _fixed(item: Any) -> list[dict[str, Any]]:
    """Fixed moments: a weekday, a sport, a name and minutes (e.g. the Tuesday group run)."""
    if not isinstance(item, list) or len(item) > MAX_FIXED:
        raise ValueError(f"plan_builder.fixed: a list of at most {MAX_FIXED}")
    out = []
    for row in item:
        if not isinstance(row, dict):
            raise ValueError("plan_builder.fixed: day, sport, name and minutes")
        day, sport, minutes = row.get("day"), row.get("sport"), row.get("minutes")
        name = str(row.get("name") or "").strip()[:80]
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ValueError("plan_builder.fixed.day: 0 (Monday) to 6 (Sunday)")
        if sport not in FIXED_SPORTS:
            raise ValueError(f"plan_builder.fixed.sport: one of {', '.join(FIXED_SPORTS)}")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 10 <= minutes <= 300:
            raise ValueError("plan_builder.fixed.minutes: 10 to 300")
        if not name:
            raise ValueError("plan_builder.fixed.name: give it a name")
        out.append({"day": day, "sport": sport, "name": name, "minutes": minutes})
    return out


def _when(start: date, kind: str, sessions: list[dict[str, Any]], current: dict[str, Any]):
    """Fixed moments in every week but race week, each taking the place of a session of its
    sport on that day; then sessions on a rest day move to the next free day of the week."""
    rest = set(current.get("rest_days") or [])
    fixed = (current.get("fixed") or []) if kind != "race" else []
    out = list(sessions)
    for moment in fixed:
        day = (start + timedelta(days=moment["day"])).isoformat()
        out = [s for s in out if not (s["date"] == day and s["sport"] == moment["sport"])]
        out.append(
            {
                "date": day,
                "sport": moment["sport"],
                "template": "fixed",
                "name": moment["name"],
                "minutes": moment["minutes"],
                "fixed": True,
            }
        )
    free = [d for d in range(7) if d not in rest]
    for s in out:
        weekday = (date.fromisoformat(s["date"]) - start).days
        if s.get("fixed") or weekday not in rest or not free:
            continue
        later = [d for d in free if d > weekday] or free
        s["date"] = (start + timedelta(days=later[0])).isoformat()
        s["moved"] = True
    return sorted(out, key=lambda s: (s["date"], s["sport"]))


def settings(engine: Any, data_dir: Any, today: date) -> dict[str, Any]:
    """The defaults from the current plan with what the athlete chose on top."""
    from vibesynergy.portal import prefs
    from vibesynergy.query import training_plan

    chosen = prefs.load(data_dir).get("plan_builder") or {}
    current = {**defaults(engine, today), **chosen}
    races = [
        {
            "id": r["source_id"],
            "title": r["title"],
            "date": str(r["date"]),
            "priority": r.get("priority"),
        }
        for r in training_plan.overview(engine, today)["races"]
    ]
    return {
        "settings": current,
        "chosen": sorted(chosen),
        "races": races,
        "choices": {
            "methods": list(METHODS),
            "sports": {k: {n: list(v) for n, v in d.items()} for k, d in SPORTS.items()},
            "rhythms": list(RHYTHMS),
            "priorities": list(PRIORITIES),
        },
    }


# The draft (part 3) ------------------------------------------------------------------
# A first plan from the settings, to look at: nothing is written to the plan. Phases after
# Friel (base, build, peak, taper, race week), the rhythm's recovery weeks, hours rising from
# the minimum to the maximum, a split over the sports by race type, and sessions from the
# workout library on a fixed weekly pattern, then your rest days and fixed moments (``_when``).
# The coach adjusts the draft.

SPLITS = {
    "long": {"swim": 0.2, "ride": 0.5, "run": 0.3},  # middle and full distance
    "short": {"swim": 0.25, "ride": 0.4, "run": 0.35},  # super sprint to olympic
    "run": {"run": 1.0},
}
RECOVERY_SHARE = 0.65
TAPER_SHARES = (0.7, 0.5, 0.5, 0.5)
RACE_SHARE = 0.4
PEAK_WEEKS = 2


def _race_date(current: dict[str, Any], races: list[dict[str, Any]]) -> date | None:
    goal = current.get("goal") or {}
    if goal.get("new"):
        return date.fromisoformat(goal["new"]["date"])
    found = next((r for r in races if r["id"] == goal.get("race")), None)
    return date.fromisoformat(found["date"][:10]) if found else None


def _phases(n: int, taper: int, rhythm: str) -> list[tuple[str, str]]:
    """(phase, kind) per week: kind is load, recovery, taper or race."""
    out: list[tuple[str, str]] = []
    taper = min(taper, max(n - 1, 0))
    peak = min(PEAK_WEEKS, max(n - 1 - taper, 0))
    rest = max(n - 1 - taper - peak, 0)
    base = round(rest * 0.6)
    cycle = 4 if rhythm == "3:1" else 3
    for i in range(rest):
        phase = "base" if i < base else "build"
        position = i if phase == "base" else i - base
        kind = "recovery" if position % cycle == cycle - 1 else "load"
        out.append((phase, kind))
    out += [("peak", "load")] * peak + [("taper", "taper")] * taper
    if n:
        out.append(("race", "race"))
    return out[-n:] if n else out


def _round_quarter(hours: float) -> float:
    return round(hours * 4) / 4


def _hours(weeks: list[tuple[str, str]], low: float, high: float, taper: int) -> list[float]:
    loading = sum(1 for p, k in weeks if p in ("base", "build") and k == "load")
    out: list[float] = []
    seen, last = 0, low
    taper_i = 0
    for phase, kind in weeks:
        if phase in ("base", "build") and kind == "load":
            last = low + (high - low) * (seen / max(loading - 1, 1))
            seen += 1
            out.append(_round_quarter(last))
        elif kind == "recovery":
            out.append(_round_quarter(last * RECOVERY_SHARE))
        elif phase == "peak":
            out.append(_round_quarter(high * 0.9))
        elif kind == "taper":
            out.append(_round_quarter(high * TAPER_SHARES[min(taper_i, len(TAPER_SHARES) - 1)]))
            taper_i += 1
        else:
            out.append(_round_quarter(high * RACE_SHARE))
    return out


# (weekday, template, share of the sport's minutes or None for the template's own length)
PATTERN: dict[str, dict[str, list[tuple[int, str, float | None]]]] = {
    "run": {
        "base": [(1, "run_easy", 0.3), (2, "run_easy", 0.25), (6, "run_long", 0.45)],
        "build": [(1, "run_intervals", None), (2, "run_easy", 0.3), (6, "run_long", 0.45)],
        "peak": [(1, "run_intervals", None), (2, "run_easy", 0.3), (6, "run_long", 0.45)],
        "recovery": [(2, "run_easy", 0.5), (6, "run_easy", 0.5)],
        "taper": [(1, "run_easy", 0.5), (5, "run_easy", 0.5)],
        "race": [(2, "run_easy", None)],
    },
    "ride": {
        "base": [(1, "ride_force", None), (5, "ride_endurance", 1.0)],
        "build": [(1, "ride_sweetspot", None), (5, "ride_endurance", 1.0)],
        "peak": [(1, "ride_sweetspot", None), (5, "ride_endurance", 1.0)],
        "recovery": [(5, "ride_endurance", 1.0)],
        "taper": [(1, "ride_endurance", 0.4), (5, "ride_endurance", 0.6)],
        "race": [(1, "ride_endurance", None)],
    },
    "swim": {
        "base": [(0, "swim_technique", None), (2, "swim_endurance", None)],
        "build": [(0, "swim_technique", None), (2, "swim_endurance", None)],
        "peak": [(0, "swim_technique", None), (2, "swim_endurance", None)],
        "recovery": [(0, "swim_technique", None)],
        "taper": [(0, "swim_technique", None), (2, "swim_endurance", None)],
        "race": [(0, "swim_technique", None)],
    },
}
TESTS = {"run": (1, "run_test"), "ride": (5, "ride_ftp_test"), "swim": (2, "swim_css_test")}
STRENGTH = {"base": ["strength_mt_a", "strength_mt_b"], "build": ["strength_ms"]}
STRENGTH_DAYS = (0, 3)


def _sessions(
    start: date, phase: str, kind: str, minutes: dict[str, float], current: dict[str, Any]
) -> list[dict[str, Any]]:
    from vibesynergy.query import workout_library

    key = kind if kind in ("recovery", "taper", "race") else phase
    out = []
    for sport, total in minutes.items():
        rows = list(PATTERN[sport][key])
        if current.get("tests") and kind == "recovery" and sport in TESTS:
            rows.append(TESTS[sport] + (None,))
        for day, template, share in rows:
            params: dict[str, Any] = {}
            defaults = workout_library.BY_ID[template].defaults
            if share is not None and "minutes" in defaults:
                params["minutes"] = max(20, int(round(total * share / 5) * 5))
            if template == "swim_technique":  # the drill focus of that week (swim_drills)
                params["week"] = (start + timedelta(days=day)).isocalendar()[1]
            done = workout_library.render(template, "nl", **params)
            out.append(
                {
                    "date": (start + timedelta(days=day)).isoformat(),
                    "sport": sport,
                    "template": template,
                    "name": done["name"],
                    "minutes": round(done["seconds"] / 60),
                }
            )
    if current.get("sport") == "triathlon" and phase in ("build", "peak") and kind == "load":
        done = workout_library.render("run_brick", "nl")
        out.append(
            {
                "date": (start + timedelta(days=5)).isoformat(),
                "sport": "run",
                "template": "run_brick",
                "name": done["name"],
                "minutes": round(done["seconds"] / 60),
            }
        )
    if current.get("strength") and kind == "load":
        for day, template in zip(STRENGTH_DAYS, STRENGTH.get(phase, []), strict=False):
            done = workout_library.render(template, "nl")
            out.append(
                {
                    "date": (start + timedelta(days=day)).isoformat(),
                    "sport": "strength",
                    "template": template,
                    "name": done["name"],
                    "minutes": round(done["seconds"] / 60),
                }
            )
    return sorted(out, key=lambda s: (s["date"], s["sport"]))


def _current_weeks(engine: Any, start: date, end: date) -> dict[str, dict[str, Any]]:
    """The current plan's weeks by Monday: phase, kind and hours from the week targets."""
    from vibesynergy.query import plan_model

    try:
        found = plan_model.view(engine, start, end)
    except Exception:
        return {}
    out = {}
    for w in found.get("weeks") or []:
        seconds = sum(t.get("time_s") or 0 for t in w.get("targets") or [])
        out[w["monday"]] = {
            "phase": w.get("phase"),
            "kind": w.get("kind"),
            "hours": round(seconds / 3600, 2) if seconds else None,
        }
    return out


def draft(engine: Any, data_dir: Any, today: date) -> dict[str, Any]:
    """A first plan from the builder's settings, week by week. Nothing is written."""
    info = settings(engine, data_dir, today)
    current = info["settings"]
    race = _race_date(current, info["races"])
    if race is None or race <= today:
        raise ValueError("choose a race in the future first")
    start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    n = min(52, (race - start).days // 7 + 1)
    phases = _phases(n, int(current.get("taper_weeks") or 2), current.get("rhythm") or "3:1")
    hours = _hours(
        phases,
        float(current.get("hours_min") or 6),
        float(current.get("hours_max") or 10),
        int(current.get("taper_weeks") or 2),
    )
    if current.get("sport") == "triathlon":
        split = SPLITS["long" if current.get("distance") in ("middle", "full") else "short"]
    else:
        split = SPLITS["run"]
    now = _current_weeks(engine, start, start + timedelta(days=7 * n))
    weeks = []
    for i, ((phase, kind), total) in enumerate(zip(phases, hours, strict=True)):
        monday = start + timedelta(days=7 * i)
        minutes = {sport: total * 60 * share for sport, share in split.items()}
        sessions = _when(monday, kind, _sessions(monday, phase, kind, minutes, current), current)
        weeks.append(
            {
                "start": monday.isoformat(),
                "phase": phase,
                "kind": kind,
                "hours": total,
                "sports": {sport: round(m / 60, 2) for sport, m in minutes.items()},
                "sessions": sessions,
                # The same week in the plan you have now, to compare (nothing is changed).
                "current": now.get(monday.isoformat()),
            }
        )
    return {"race_date": race.isoformat(), "start": start.isoformat(), "weeks": weeks}
