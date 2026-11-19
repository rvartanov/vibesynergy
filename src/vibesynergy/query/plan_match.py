"""The planned workout against what was done, by content, not only by size (#116).

``plan.deviation`` compares the size of a session (duration, distance, time above the
planned zone). Two sessions of the same size can still be different workouts: 3x300 Middel
planned, 4x50 drills + 3x100 + 4x(200+50) done. This module reads the planned steps
(``workout_model`` steps) and the activity's laps, and says three things:

- ``planned``: the main set as text, ``3x300 Middel`` or ``25' Z2`` (warm-up, cool-down and
  rests left out);
- ``done``: the main sets that were done, from the laps, as text (``4x50 + 3x100``);
- ``check``: ``match`` (the laps show the planned structure), ``differs`` (they clearly show
  another one: the session is *changed*), or ``not_checked`` (the laps are too thin to judge:
  no laps, one lap, or auto laps that cannot show the planned intervals). ``None``: there is
  nothing to compare (a strength session, a training you give, a plan without steps).

Swim: the work laps are the laps with a distance (a rest lap has none). Garmin pool laps
can be wrong by a length or have an odd speed (the "meetfout" of 18-11, KB
``swim-data-quality``): distances count as the same within one pool length (25 m) or 10%,
speed is not used, and the verdict is by share, so a few bad laps do not flip it.

Run and ride: a lap is *hard* when its speed (run) or power (ride) is at least 12% above the
session's time-weighted median; by speed, its heart rate, when known, must be at least 5 bpm
above the median too (a fast downhill lap is not hard). Hard laps next to each other are
one block. A steady plan with 8 minutes or more of hard blocks (and at least 15% of the
session) differs; so does an interval plan done steady, or with clearly other reps, when the
laps follow the watch's workout steps. Auto laps (every lap about the same distance) cannot
show short intervals: an interval plan with only auto laps is not checked.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.store.schema import activity_lap

POOL_M = 25.0  # one length: a lap may be off by this much
_LENGTH_SHARE = 0.10
_DURATION_SHARE = 0.20
HARD_RATIO = 1.12
HARD_HR = 5.0
HARD_MIN_S = 480.0
HARD_SHARE = 0.15
MIN_LAP_S = 30.0
PLANNED_SHARE = 0.5  # below this share of the planned main set found in the laps: differs
DONE_SHARE = 0.3  # below this share of the done main set being planned reps: differs
MAX_GROUPS = 4  # sets shown in a text; more become " + …"
_CUE_MAX = 12  # a swim cue short enough to be an intensity word ("Middel", "Licht")
_EDGE = {"warmup", "cooldown"}
_REST = {"rest", "recovery"}
CHECKED_FAMILIES = ("swim", "run", "ride")


# Planned ---------------------------------------------------------------------------------


def _label(step: dict[str, Any], fam: str | None) -> str:
    cue = str(step.get("cue") or "").strip()
    if fam == "swim" and cue and len(cue) <= _CUE_MAX:
        return cue
    target = step.get("target") or {}
    kind, low, high = target.get("type"), target.get("low"), target.get("high")
    rng = f"{low}" if low == high else f"{low}-{high}"
    if kind in ("hr_zone", "pace_zone"):
        return f"Z{low}" if low == high else f"Z{low}-Z{high}"
    if kind == "pct_ftp":
        return f"{rng}%"
    if kind == "pct_lthr":
        return f"{rng}% LTHR"
    return ""


def size_text(step: dict[str, Any]) -> str:
    """``300`` (swim metres), ``1 km``, ``400 m``, ``25'``, ``4'30"`` or ``45"``."""
    if step.get("distance_m") is not None:
        return _metres(float(step["distance_m"]), swim=step.get("_swim", False))
    return minutes_text(float(step.get("duration_s") or 0))


def _metres(value: float, *, swim: bool) -> str:
    if swim:
        return f"{round(value):d}"
    if value >= 1000:
        return f"{round(value / 1000, 1):g} km"
    return f"{round(value):d} m"


def minutes_text(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f'{seconds}"'
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}'" if not rest else f"{minutes}'{rest:02d}\""


def _size(step: dict[str, Any]) -> tuple[str, float]:
    if step.get("distance_m") is not None:
        return "m", float(step["distance_m"])
    return "s", float(step.get("duration_s") or 0)


def _main_units(steps: list[dict[str, Any]], fam: str | None) -> list[dict[str, Any]]:
    """The main set as units: ``{"times", "parts": [step…]}``. A repeat is one unit; work
    steps outside a repeat are units of one. Warm-up, cool-down and rests are left out."""
    units: list[dict[str, Any]] = []
    for step in steps:
        kind = step.get("kind")
        if kind == "repeat":
            parts = [s for s in step.get("steps") or [] if s.get("kind") not in _REST | _EDGE]
            if parts:
                units.append({"times": int(step.get("times") or 1), "parts": parts})
        elif kind not in _REST | _EDGE:
            units.append({"times": 1, "parts": [step]})
    for unit in units:
        for part in unit["parts"]:
            part["_swim"] = fam == "swim"
    return units


def _unit_key(unit: dict[str, Any], fam: str | None) -> tuple:
    return tuple((_size(p), _label(p, fam)) for p in unit["parts"])


def _unit_text(times: int, parts: list[dict[str, Any]], fam: str | None) -> str:
    labels = {_label(p, fam) for p in parts}
    common = labels.pop() if len(labels) == 1 else None
    if len(parts) == 1:
        body = size_text(parts[0])
    elif common is not None:
        body = "(" + " + ".join(size_text(p) for p in parts) + ")"
    else:
        body = "(" + " + ".join(_join(size_text(p), _label(p, fam)) for p in parts) + ")"
    head = f"{times}x{body}" if times > 1 else body
    if len(parts) == 1 or common is not None:
        return _join(head, common if common is not None else _label(parts[0], fam))
    return head


def _join(*bits: str | None) -> str:
    return " ".join(b for b in bits if b)


def _cap(texts: list[str]) -> str:
    shown = texts[:MAX_GROUPS]
    return " + ".join(shown) + (" + …" if len(texts) > MAX_GROUPS else "")


def planned_text(steps: list[dict[str, Any]] | None, fam: str | None) -> str | None:
    """The planned main set, ``3x300 Middel`` or ``25' Z2``; ``None`` when the steps say no
    more than the title (one block without a target, such as a strength session)."""
    units = _main_units(_copy(steps), fam)
    if not units:
        return None
    # Units of one in a row with the same size and label read as one set: 6x50 Z2.
    groups: list[list[Any]] = []
    for unit in units:
        key = _unit_key(unit, fam)
        last = groups[-1] if groups else None
        if last and unit["times"] == 1 and last[1] == key and last[2]:
            last[0] += 1
        else:
            groups.append([unit["times"], key, unit["times"] == 1, unit["parts"]])
    texts = [_unit_text(times, parts, fam) for times, _key, _single, parts in groups]
    if len(texts) == 1 and groups[0][0] == 1 and not any(_label(p, fam) for p in groups[0][3]):
        return None
    return _cap(texts)


def _copy(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for step in steps or []:
        step = dict(step)
        if step.get("steps"):
            step["steps"] = [dict(s) for s in step["steps"]]
        out.append(step)
    return out


def _reps(units: list[dict[str, Any]]) -> list[tuple[str, float]]:
    out = []
    for unit in units:
        for _ in range(unit["times"]):
            out.extend(_size(p) for p in unit["parts"])
    return out


def _has(steps: list[dict[str, Any]], kind: str) -> bool:
    return any(s.get("kind") == kind for s in steps)


def _intervals(units: list[dict[str, Any]]) -> bool:
    """An interval plan: a repeat, or work blocks with different targets."""
    if any(u["times"] > 1 for u in units):
        return True
    labels = {_label(p, None) for u in units for p in u["parts"]}
    return len(units) > 1 and len(labels) > 1


# Laps -----------------------------------------------------------------------------------


def laps_for(engine: Engine, groups: Iterable[dict[str, Any]]) -> dict[str, list[dict]]:
    """``{activity id: laps}`` for activities (``id``, ``sources``): the laps of the first
    member that has any, Garmin first. Missing laps give no entry."""
    wanted: dict[str, list[tuple[str, str]]] = {}
    for group in groups:
        refs = [str(r) for r in group.get("sources") or [] if ":" in str(r)]
        refs.sort(key=lambda r: not r.startswith("garmin:"))
        wanted[group["id"]] = [tuple(r.split(":", 1)) for r in refs]  # type: ignore[misc]
    ids = sorted({sid for pairs in wanted.values() for _src, sid in pairs})
    if not ids:
        return {}
    found: dict[tuple[str, str], list[dict]] = {}
    with engine.connect() as conn:
        for row in conn.execute(
            select(
                activity_lap.c.source,
                activity_lap.c.activity_source_id,
                activity_lap.c.lap_index,
                activity_lap.c.elapsed_s,
                activity_lap.c.moving_s,
                activity_lap.c.distance_m,
                activity_lap.c.avg_hr,
                activity_lap.c.avg_power,
                activity_lap.c.avg_speed,
                activity_lap.c.extra,
            )
            .where(activity_lap.c.activity_source_id.in_(ids))
            .order_by(activity_lap.c.lap_index)
        ).mappings():
            extra = row["extra"] or {}
            found.setdefault((row["source"], row["activity_source_id"]), []).append(
                {
                    "index": row["lap_index"],
                    "seconds": float(row["moving_s"] or row["elapsed_s"] or 0),
                    "distance_m": float(row["distance_m"] or 0),
                    "hr": row["avg_hr"],
                    "power": row["avg_power"],
                    "speed": row["avg_speed"],
                    "step": extra.get("wktStepIndex") if isinstance(extra, dict) else None,
                }
            )
    out = {}
    for gid, pairs in wanted.items():
        for pair in pairs:
            if found.get(pair):
                out[gid] = found[pair]
                break
    return out


def _same(a: float, b: float, unit: str) -> bool:
    if unit == "m":
        return abs(a - b) <= max(POOL_M, _LENGTH_SHARE * max(a, b))
    return abs(a - b) <= max(15.0, _DURATION_SHARE * max(a, b))


def _mode(values: list[float]) -> float:
    return max(set(values), key=lambda v: (values.count(v), -v))


def sets_text(values: list[float], unit: str, *, swim: bool = True) -> str | None:
    """Laps as sets: ``4x50 + 3x100 + 4x(200+50) + 6x50``. Equal within a length; a pair
    that repeats (200, 50, 200, 50) is one set. Each size is the most common one."""
    if not values:
        return None
    fmt = (lambda v: _metres(v, swim=swim)) if unit == "m" else minutes_text
    texts, i = [], 0
    while i < len(values):
        best = (1, 1)  # (period, times)
        for period in (1, 2):
            pattern = values[i : i + period]
            if len(pattern) < period:
                continue
            times = 1
            while i + (times + 1) * period <= len(values) and all(
                _same(values[i + times * period + k], pattern[k], unit) for k in range(period)
            ):
                times += 1
            if times > 1 and times * period > best[0] * best[1]:
                best = (period, times)
        period, times = best
        chunk = values[i : i + period * times]
        parts = [_mode([chunk[k] for k in range(j, len(chunk), period)]) for j in range(period)]
        body = fmt(parts[0]) if period == 1 else "(" + "+".join(fmt(p) for p in parts) + ")"
        texts.append(f"{times}x{body}" if times > 1 else body)
        i += period * times
    return _cap(texts)


# Compare --------------------------------------------------------------------------------


def compare(
    steps: list[dict[str, Any]] | None,
    fam: str | None,
    laps: list[dict[str, Any]] | None,
    *,
    coached: bool = False,
) -> dict[str, Any]:
    """``{"check", "planned", "done"}`` for a planned session and the laps of the activity
    that did it (module docstring). ``done_shape`` is ``steady`` when a run or ride shows no
    hard blocks."""
    planned = planned_text(steps, fam)
    out: dict[str, Any] = {"check": None, "planned": planned, "done": None}
    units = _main_units(_copy(steps), fam)
    if not steps or not units or fam not in CHECKED_FAMILIES:
        out["done"] = _done_only(fam, laps)
        return out
    if coached:
        # A training you give: the group's structure, you run along easy.
        return out
    if fam == "swim" or all(unit == "m" for unit, _v in _reps(units)):
        return out | _compare_distance(steps, units, laps or [], fam)
    return out | _compare_effort(units, laps or [], fam)


def _done_only(fam: str | None, laps: list[dict[str, Any]] | None) -> str | None:
    if fam == "swim" and laps:
        work = [lap["distance_m"] for lap in laps if lap["distance_m"] > 0]
        return sets_text(work, "m") if len(work) > 1 else None
    return None


def _compare_distance(steps, units, laps, fam) -> dict[str, Any]:
    swim = fam == "swim"
    work = [lap["distance_m"] for lap in laps if lap["distance_m"] > 0]
    if len(work) < 2:
        return {"check": "not_checked"}
    main = list(work)
    if len(main) >= 4 and _has(steps, "warmup"):
        main = main[1:]
    if len(main) >= 3 and _has(steps, "cooldown"):
        main = main[:-1]
    done = sets_text(main, "m", swim=swim)
    reps = [value for unit, value in _reps(units) if unit == "m"]
    planned_total = sum(reps)
    done_total = sum(main)
    if not reps or planned_total <= 0 or done_total <= 0:
        return {"check": "not_checked", "done": done}
    # One long lap for a set of reps: the watch did not see the rests. Too thin to judge.
    if len(main) < len(reps) and max(main) > 1.5 * max(reps):
        return {"check": "not_checked", "done": done}
    sizes = sorted(set(reps))
    found = 0.0
    pool = list(main)
    for rep in reps:
        hit = next((k for k, value in enumerate(pool) if _same(value, rep, "m")), None)
        if hit is not None:
            found += rep
            pool.pop(hit)
    planned_reps = sum(v for v in main if any(_same(v, s, "m") for s in sizes))
    differs = found / planned_total < PLANNED_SHARE or planned_reps / done_total < DONE_SHARE
    return {"check": "differs" if differs else "match", "done": done}


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    pairs = sorted((v, w) for v, w in pairs if v is not None and v > 0 and w > 0)
    total = sum(w for _v, w in pairs)
    if not pairs or total <= 0:
        return None
    seen = 0.0
    for value, weight in pairs:
        seen += weight
        if seen >= total / 2:
            return value
    return pairs[-1][0]


def _hard_blocks(laps: list[dict[str, Any]], fam: str | None) -> list[float] | None:
    """Seconds of each block of hard laps; ``None`` when the laps carry no speed or power."""
    use = [lap for lap in laps if lap["seconds"] >= MIN_LAP_S]
    key = (
        "power"
        if fam == "ride" and sum(bool(lap["power"]) for lap in use) * 2 > len(use)
        else "speed"
    )
    base = _weighted_median([(lap[key], lap["seconds"]) for lap in use])
    if base is None:
        return None
    # Speed can be the terrain (a fast downhill lap): it counts as hard only with the heart
    # rate up too. Power is the effort itself.
    hr = None
    if key == "speed":
        hr = _weighted_median([(lap["hr"], lap["seconds"]) for lap in use if lap["hr"]])
    blocks: list[float] = []
    open_block = False
    for lap in laps:
        if lap["seconds"] < MIN_LAP_S:
            continue  # a lap press or a glitch neither starts nor ends a block
        value = lap[key]
        hard = (
            value is not None
            and value >= HARD_RATIO * base
            and (hr is None or not lap["hr"] or lap["hr"] >= hr + HARD_HR)
        )
        if hard and open_block:
            blocks[-1] += lap["seconds"]
        elif hard:
            blocks.append(lap["seconds"])
        open_block = hard
    return blocks


def _auto_laps(laps: list[dict[str, Any]]) -> bool:
    """Every lap but the last about the same distance: the watch's auto lap, not steps."""
    if any(lap.get("step") is not None for lap in laps):
        return False
    body = [lap["distance_m"] for lap in laps[:-1]]
    if len(body) < 2 or not body[0]:
        return False
    return all(abs(d - body[0]) <= 0.03 * body[0] for d in body)


def _compare_effort(units, laps, fam) -> dict[str, Any]:
    use = [lap for lap in laps if lap["seconds"] >= MIN_LAP_S]
    if len(use) < 3:
        return {"check": "not_checked"}
    blocks = _hard_blocks(laps, fam)
    if blocks is None:
        return {"check": "not_checked"}
    total = sum(lap["seconds"] for lap in use)
    hard_s = sum(blocks)
    done = sets_text(blocks, "s") if blocks else None
    shape = None if blocks else "steady"
    if not _intervals(units):
        differs = hard_s >= HARD_MIN_S and hard_s >= HARD_SHARE * total
        if not differs:
            # A short pick-up in a steady session is that session: no sets to name.
            return {"check": "match", "done": None, "done_shape": "steady"}
        return {"check": "differs", "done": done, "done_shape": shape}
    if _auto_laps(laps):
        return {"check": "not_checked", "done": done, "done_shape": shape}
    reps = [
        value
        for unit in units
        if unit["times"] > 1
        for _ in range(unit["times"])
        for kind, value in [_size(unit["parts"][0])]
        if kind == "s"
    ]
    if not reps:
        return {"check": "not_checked", "done": done, "done_shape": shape}
    if not blocks:
        return {"check": "differs", "done": None, "done_shape": "steady"}
    planned_mean = sum(reps) / len(reps)
    done_mean = hard_s / len(blocks)
    off_count = abs(len(blocks) - len(reps)) > max(1, 0.3 * len(reps))
    off_size = abs(done_mean - planned_mean) > 0.4 * planned_mean
    return {"check": "differs" if off_count or off_size else "match", "done": done}


# For the AI coach -----------------------------------------------------------------------


def done_line(item: dict[str, Any]) -> str | None:
    """What was done for a matched planned session, as one line: ``lap_swimming 2525 m,
    68' — 5x50 + 3x100 · RPE 4``. ``None`` without a matched activity."""
    found = (item.get("done_by") or [None])[0]
    if not found:
        return None
    comparison = item.get("comparison") or {}
    sport = found.get("sport") or item.get("sport") or ""
    bits = []
    distance = found.get("distance_m")
    if isinstance(distance, (int, float)) and distance > 0:
        swim = _is_swim(sport)
        bits.append(f"{round(distance)} m" if swim else _metres(float(distance), swim=False))
    seconds = found.get("moving_s") or found.get("elapsed_s")
    if isinstance(seconds, (int, float)) and seconds > 0:
        bits.append(f"{round(seconds / 60)}'")
    text = _join(sport, ", ".join(bits))
    if comparison.get("done"):
        text += f" — {comparison['done']}"
    elif comparison.get("done_shape") == "steady":
        text += " — steady"
    if isinstance(comparison.get("rpe"), (int, float)):
        text += f" · RPE {comparison['rpe']:g}"
    return text


def _is_swim(sport: str) -> bool:
    return "swim" in sport.lower()


def planned_line(item: dict[str, Any]) -> str:
    """The planned session as one line: its title, with the main set when the steps say
    more: ``Zwemmen duur 40' — 3x300 Middel``."""
    title = str(item.get("title") or item.get("name") or item.get("sport") or "")
    planned = (item.get("comparison") or {}).get("planned")
    return f"{title} — {planned}" if planned else title


def for_coach(item: dict[str, Any]) -> dict[str, Any]:
    """Planned against done for the AI coach (``today``, ``plan_match``): the two lines, the
    check, and the deviation that made the session *changed*. A session with ``check``
    ``not_checked`` was done, but whether it was done as prescribed is not known; one with
    a ``structure`` deviation was another workout than planned."""
    comparison = item.get("comparison") or {}
    return {
        "planned": planned_line(item),
        "done": done_line(item),
        "check": comparison.get("check"),
        "deviation": item.get("deviation") if item.get("status") == "changed" else None,
    }
