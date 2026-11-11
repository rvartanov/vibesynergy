"""Strength and pilates reach Garmin with real exercise steps.

intervals.icu pushes a strength session to Garmin as one free step, so the watch shows a
single "Other" block without exercises. The plan therefore carries the session's exercises
in a ``[garmin]`` block in the intervals.icu description, one line per step, with Garmin's
own exercise keys::

    [garmin]
    warmup time=5m "Warming-up: 5' rustig fietsen of roeien"
    SQUAT/GOBLET_SQUAT sets=2 reps=15 rest=75s "Goblet squat"
    PLANK/SIDE_PLANK sets=3 time=30s side=both rest=30s "Zijplank"

or, for a group class without exercises, ``pilates "Pilatesles"`` (also ``yoga``): one open
step that ends when the athlete stops it, so no 60-minute countdown runs during the class.
The block ends at the first blank line. Key=value keeps the lines clear of intervals.icu's
own step syntax (``- 10m``, ``4x``), so its parser leaves them as text.

``parse_block`` reads and checks the block against Garmin's exercise catalog; the plan
writes refuse an invalid one. ``run`` (a scheduler service) builds the Garmin workout for
each upcoming session with a block and schedules it on that day under the session's name.
Until 7-11-2026 an emoji (``NAME_PREFIX``) set it apart from intervals.icu's own Garmin copy,
which stayed. the athlete switched intervals.icu's Garmin push off then; the copies it
had already pushed stay on Garmin under the same name, so ``_drop_copies`` takes a same-day,
same-name workout that is not ours off the calendar. The planned load stays on the
intervals.icu event, which still matches the activity.
``garmin_strength_delivery`` remembers what was delivered, so
an edit or a move in intervals.icu replaces ours, a removed block or event takes ours off the
calendar, and a re-push by intervals.icu is taken off again. Running, cycling and swimming
stay with intervals.icu.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.query import evidence
from vibesynergy.store.schema import (
    dataset_state,
    garmin_strength_delivery,
    icu_event,
    planned_item,
)

log = logging.getLogger(__name__)

JOB = "garmin_strength"
MARKER = "[garmin]"
LOOKAHEAD_DAYS = 21
# A calendar sync this long after a delivery has had time to normalise it.
SEEN_AFTER = timedelta(minutes=10)
DESCRIPTION_MAX = 1000
CLASSES = {"pilates": (8, "pilates"), "yoga": (7, "yoga")}
STRENGTH_SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations"}
_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
_STEP_TYPES = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "interval": (3, "interval"),
    "recovery": (4, "recovery"),
}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h)?$")
_SIDES = {"both": ("links", "rechts"), "none": ()}


class BlockError(ValueError):
    """A ``[garmin]`` block that cannot become a Garmin workout."""


@dataclass
class Step:
    kind: str  # warmup, cooldown, exercise, class
    label: str = ""
    category: str | None = None
    name: str | None = None
    sets: int = 1
    reps: int | None = None
    seconds: int | None = None
    rest: int = 0
    sides: tuple[str, ...] = field(default_factory=tuple)
    weight: tuple[float, str] | None = None  # (value, "kg" or "lb"); None = empty field


@dataclass
class Block:
    steps: list[Step]
    sport: str = "strength_training"  # or a class sport: pilates, yoga


# Parsing ------------------------------------------------------------------------------------


def block_lines(description: str | None) -> list[str] | None:
    """The block's lines, or None when the description has no ``[garmin]`` marker."""
    lines = (description or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == MARKER:
            out: list[str] = []
            for rest in lines[index + 1 :]:
                if not rest.strip():
                    break
                out.append(rest.strip())
            return out
    return None


def intro(description: str | None) -> str:
    """The text Garmin shows: the description's first paragraph, without the block."""
    lines: list[str] = []
    for line in (description or "").splitlines():
        text = line.strip()
        if not text or text.lower() == MARKER or re.match(r"^(\d+\.|-)\s", text):
            break
        lines.append(text)
    return " ".join(lines)[:DESCRIPTION_MAX]


def _seconds(value: str, line: str) -> int:
    match = _DURATION.match(value.strip().lower())
    if not match:
        raise BlockError(f"{line!r}: {value!r} is not a duration such as 30s, 5m or 1h.")
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    seconds = round(amount * {"s": 1, "m": 60, "h": 3600}[unit])
    if seconds <= 0:
        raise BlockError(f"{line!r}: a duration must be more than zero.")
    return seconds


_WEIGHT = re.compile(r"^(\d+(?:[.,]\d+)?)(kg|lb)$")
# Garmin's own weight units on a workout step (weightValue is in that unit).
_WEIGHT_UNITS = {
    "kg": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0},
    "lb": {"unitId": 9, "unitKey": "pound", "factor": 453.59237},
}


def _weight(value: str, line: str) -> tuple[float, str] | None:
    """weight=16kg, 27.5kg, 35lb, or bw (own body weight: the field stays empty)."""
    text = value.strip().lower()
    if text == "bw":
        return None
    match = _WEIGHT.match(text)
    if not match or float(match.group(1).replace(",", ".")) <= 0:
        raise BlockError(f"{line!r}: {value!r} is not a weight such as 16kg, 27.5kg, 35lb or bw.")
    return float(match.group(1).replace(",", ".")), match.group(2)


def _count(value: str, key: str, line: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise BlockError(f"{line!r}: {key} must be a whole number of at least 1.")
    return int(value)


def _split(line: str) -> tuple[str, dict[str, str], str]:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise BlockError(f"{line!r}: {exc}.") from exc
    if not parts:
        raise BlockError("An empty block line.")
    head, options, label = parts[0], {}, ""
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        if sep:
            options[key.strip().lower()] = value.strip()
        elif not label:
            label = part
        else:
            raise BlockError(f"{line!r}: put the text in one pair of quotes.")
    return head, options, label


def _known(options: dict[str, str], allowed: set[str], line: str) -> None:
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise BlockError(
            f"{line!r}: unknown {', '.join(unknown)}; use {', '.join(sorted(allowed))}."
        )


def _exercise(head: str, line: str) -> tuple[str, str]:
    from vibesynergy.query.workouts import _load_catalog

    labels, _ = _load_catalog()
    category, sep, name = head.upper().partition("/")
    if sep:
        if (category, name) not in labels:
            raise BlockError(f"{line!r}: {head} is not in Garmin's exercise library.")
        return category, name
    matches = sorted(cat for cat, ex in labels if ex == category)
    if len(matches) == 1:
        return matches[0], category
    if not matches:
        raise BlockError(f"{line!r}: {head} is not in Garmin's exercise library.")
    choices = ", ".join(f"{cat}/{category}" for cat in matches)
    raise BlockError(f"{line!r}: {head} is in more than one category; write one of {choices}.")


def parse_block(description: str | None) -> Block | None:
    """The block as steps, or None without a marker. Raises BlockError when it is invalid."""
    lines = block_lines(description)
    if lines is None:
        return None
    if not lines:
        raise BlockError("The [garmin] block is empty; put one step per line under it.")
    steps: list[Step] = []
    sport = "strength_training"
    for line in lines:
        head, options, label = _split(line)
        word = head.lower()
        if word in CLASSES:
            _known(options, {"time"}, line)
            if len(lines) != 1:
                raise BlockError(f"{line!r}: a {word} class is the only line in its block.")
            # A group class runs as long as the class does: time= is accepted (older
            # blocks carry it) but the step stays open.
            if "time" in options:
                _seconds(options["time"], line)
            sport = word
            steps.append(Step("class", label or word.capitalize()))
            continue
        if word in ("warmup", "cooldown"):
            _known(options, {"time"}, line)
            if "time" not in options:
                raise BlockError(f"{line!r}: give the {word} a time, such as time=5m.")
            steps.append(Step(word, label, seconds=_seconds(options["time"], line)))
            continue
        _known(options, {"sets", "reps", "time", "rest", "side", "weight"}, line)
        category, name = _exercise(head, line)
        if ("reps" in options) == ("time" in options):
            raise BlockError(f"{line!r}: give either reps=… or time=…, not both or neither.")
        side = options.get("side", "none").lower()
        if side not in _SIDES:
            raise BlockError(f"{line!r}: side is both or none.")
        steps.append(
            Step(
                "exercise",
                label,
                category=category,
                name=name,
                sets=_count(options.get("sets", "1"), "sets", line),
                reps=_count(options["reps"], "reps", line) if "reps" in options else None,
                seconds=_seconds(options["time"], line) if "time" in options else None,
                rest=_seconds(options["rest"], line) if "rest" in options else 0,
                sides=_SIDES[side],
                weight=_weight(options["weight"], line) if "weight" in options else None,
            )
        )
    if sport == "strength_training" and not any(step.kind == "exercise" for step in steps):
        raise BlockError("The [garmin] block has no exercise; add at least one.")
    return Block(steps, sport)


# The kit in Garmin's exercise name, and the words that name it in a step text (NL and EN).
_KIT_WORDS = {
    "barbell": ("barbell", "stang"),
    "dumbbell": ("dumbbell", "halter"),
    "kettlebell": ("kettlebell",),
}


def _kit_of_name(name: str) -> str | None:
    return next((kit for kit in _KIT_WORDS if kit.upper() in name), None)


def _kits_in_text(text: str) -> set[str]:
    low = text.lower()
    return {kit for kit, words in _KIT_WORDS.items() if any(w in low for w in words)}


def check_description(description: str | None) -> None:
    """For the plan writes: refuse a description whose block would not reach Garmin, or
    whose step text names other kit than the Garmin exercise (a barbell
    Bulgarian split squat that said "dumbbells" in its text)."""
    block = parse_block(description)
    for step in block.steps if block else []:
        if step.kind != "exercise" or not step.name:
            continue
        kit = _kit_of_name(step.name)
        said = _kits_in_text(step.label or "")
        if kit and said and kit not in said:
            other = " or ".join(sorted(said))
            raise BlockError(
                f"{step.category}/{step.name}: the text says {other} but the exercise is a "
                f"{kit} one. Pick the {other} exercise, or change the text."
            )


# Building -----------------------------------------------------------------------------------


def _step(
    kind: str, description: str, *, seconds: int | None = None, reps: int | None = None
) -> dict:
    type_id, type_key = _STEP_TYPES[kind]
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
        "targetType": dict(_NO_TARGET),
    }
    if description:
        step["description"] = description[:DESCRIPTION_MAX]
    if reps is not None:
        step["endCondition"] = dict(_REPS)
        step["endConditionValue"] = float(reps)
    else:
        step["endCondition"] = dict(_TIME)
        step["endConditionValue"] = float(seconds or 0)
    return step


def _exercise_steps(step: Step) -> list[dict]:
    sides = step.sides or ("",)
    work: list[dict] = []
    for side in sides:
        text = f"{step.label}, {side}" if side and step.label else step.label or side
        entry = _step("interval", text, seconds=step.seconds, reps=step.reps)
        entry["category"] = step.category
        entry["exerciseName"] = step.name
        if step.weight:
            entry["weightValue"] = step.weight[0]
            entry["weightUnit"] = dict(_WEIGHT_UNITS[step.weight[1]])
        work.append(entry)
    if step.rest:
        work.append(_step("recovery", f"Rust {step.rest}s", seconds=step.rest))
    if step.sets == 1:
        return work
    return [
        {
            "type": "RepeatGroupDTO",
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "numberOfIterations": step.sets,
            "smartRepeat": False,
            "endCondition": dict(_ITERATIONS),
            "workoutSteps": work,
        }
    ]


def _number(steps: list[dict], start: int = 1) -> int:
    order = start
    for step in steps:
        step["stepOrder"] = order
        order += 1
        if step["type"] == "RepeatGroupDTO":
            order = _number(step["workoutSteps"], order)
    return order


def _lap_recovery() -> dict:
    step = _step("recovery", "")
    step["endCondition"] = dict(_LAP_BUTTON)
    step.pop("endConditionValue", None)
    return step


def build_workout(name: str, description: str | None, block: Block) -> dict[str, Any]:
    """Garmin Connect workout JSON for one session."""
    if block.sport in CLASSES:
        type_id, type_key = CLASSES[block.sport]
        sport = {"sportTypeId": type_id, "sportTypeKey": type_key}
    else:
        sport = dict(STRENGTH_SPORT)
    steps: list[dict] = []
    previous: str | None = None
    station: tuple | None = None
    for step in block.steps:
        if step.kind == "exercise":
            # Time to walk to the next station and set up: Garmin's own "Herstel" step that
            # waits for the lap button, after the warm-up and between exercises. A
            # warm-up set and a work set of the same exercise are one station: no pause.
            here = (step.category, step.name)
            if previous == "warmup" or (previous == "exercise" and here != station):
                steps.append(_lap_recovery())
            station = here
            steps.extend(_exercise_steps(step))
        elif step.kind == "class":
            open_step = _step("interval", step.label)
            open_step["endCondition"] = dict(_LAP_BUTTON)
            open_step.pop("endConditionValue", None)
            steps.append(open_step)
        else:
            steps.append(_step(step.kind, step.label, seconds=step.seconds))
        previous = step.kind
    _number(steps)
    workout: dict[str, Any] = {
        "workoutName": name,
        "sportType": sport,
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(sport), "workoutSteps": steps}],
    }
    if text := intro(description):
        workout["description"] = text
    return workout


def fingerprint(name: str, day: date, block: Block, description: str | None) -> str:
    return evidence.digest(
        {
            "name": name,
            "date": day.isoformat(),
            "intro": intro(description),
            "sport": block.sport,
            # Without a weight a step hashes as before weight= existed (no redelivery).
            "steps": [
                {k: v for k, v in asdict(step).items() if k != "weight" or v is not None}
                for step in block.steps
            ],
            # Lap-button recovery between stations (5 Nov 2026): redeliver once.
            "transitions": 2,  # 2: one station, one pause
            # Classes became one open step (2 Nov 2026): redeliver the timed ones once.
            **({"open": True} if block.sport in CLASSES else {}),
        }
    )


# Delivery -----------------------------------------------------------------------------------


def _garmin_connected(rt) -> bool:
    try:
        return "garmin" in rt.registry.enabled_ids() and rt.registry.get("garmin").configured()
    except Exception:
        return False


def due(rt) -> bool:
    return _garmin_connected(rt)


def _api(rt):
    from vibesynergy.sources.garmin.client import connect

    return connect(SecretStore(rt.settings.secrets_dir, "garmin"))


# Our workout's name starts with this on the watch; intervals.icu's copy keeps the plain name.
# Plain names since intervals.icu's Garmin push is off.
NAME_PREFIX = {"strength_training": "", "pilates": "", "yoga": ""}


def desired(engine, today: date, lookahead: int = LOOKAHEAD_DAYS) -> dict[str, dict[str, Any]]:
    """Upcoming plan workouts with a valid block, by intervals.icu event id."""
    out: dict[str, dict[str, Any]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(icu_event.c.id, icu_event.c.date, icu_event.c.name, icu_event.c.description)
            .where(
                icu_event.c.category == "WORKOUT",
                icu_event.c.date >= today,
                icu_event.c.date <= today + timedelta(days=lookahead),
            )
            .order_by(icu_event.c.date)
        ).all()
    for row in rows:
        try:
            block = parse_block(row.description)
        except BlockError as exc:
            log.info("Garmin strength: event %s has an invalid block (%s)", row.id, exc)
            continue
        if block is None:
            continue
        name = NAME_PREFIX.get(block.sport, "") + ((row.name or "").strip() or "Kracht")
        out[str(row.id)] = {
            "event_id": str(row.id),
            "date": row.date,
            "name": name,
            "workout": build_workout(name, row.description, block),
            "fingerprint": fingerprint(name, row.date, block, row.description),
        }
    return out


def _deliveries(engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(garmin_strength_delivery)).mappings().all()
    return {row["icu_event_id"]: dict(row) for row in rows}


def _save(engine, event_id: str, **values: Any) -> None:
    values["updated_at"] = utc_now()
    with engine.begin() as conn:
        found = conn.execute(
            select(garmin_strength_delivery.c.icu_event_id).where(
                garmin_strength_delivery.c.icu_event_id == event_id
            )
        ).first()
        if found:
            conn.execute(
                garmin_strength_delivery.update()
                .where(garmin_strength_delivery.c.icu_event_id == event_id)
                .values(**values)
            )
        else:
            conn.execute(garmin_strength_delivery.insert().values(icu_event_id=event_id, **values))


def _garmin_day(engine, day: date) -> list[dict[str, Any]]:
    """Garmin's scheduled workouts on a day, as the last calendar sync saw them."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(planned_item.c.source_id, planned_item.c.title, planned_item.c.extra).where(
                planned_item.c.source == "garmin",
                planned_item.c.kind == "workout",
                planned_item.c.date == day,
            )
        ).all()
    out = []
    for row in rows:
        _, _, ident = str(row.source_id).partition(":")
        if ident.isdigit():
            workout_id = (row.extra or {}).get("workoutId")
            out.append(
                {
                    "scheduled_id": int(ident),
                    "title": row.title or "",
                    "workout_id": _as_int(workout_id),
                }
            )
    return out


def _calendar_synced_at(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(dataset_state.c.last_success_at).where(
                dataset_state.c.source == "garmin",
                dataset_state.c.dataset == "scheduled_workouts",
            )
        ).scalar()


def _went_missing(engine, row: dict[str, Any], day: date, calendar_at) -> bool:
    """A delivered workout that a later Garmin calendar sync no longer lists."""
    if row.get("state") != "delivered" or not row.get("scheduled_id") or calendar_at is None:
        return False
    updated = row.get("updated_at")
    if updated is None or calendar_at < updated + SEEN_AFTER:
        return False
    listed = {item["scheduled_id"] for item in _garmin_day(engine, day)}
    return int(row["scheduled_id"]) not in listed


def _gone(exc: Exception) -> bool:
    text = str(exc)
    return "404" in text or "NotFound" in text


def _unschedule(api, scheduled_id: int) -> None:
    try:
        api.unschedule_workout(scheduled_id)
    except Exception as exc:
        if not _gone(exc):
            raise


def _delete(api, workout_id: int) -> None:
    try:
        api.delete_workout(workout_id)
    except Exception as exc:
        if not _gone(exc):
            raise


def _withdraw(api, row: dict[str, Any]) -> None:
    """Take a delivered workout off the calendar and out of the library (both are ours)."""
    if row.get("scheduled_id"):
        _unschedule(api, int(row["scheduled_id"]))
    if row.get("workout_id"):
        _delete(api, int(row["workout_id"]))


def _drop_copies(engine, api, wanted: dict[str, dict]) -> int:
    """intervals.icu's earlier Garmin pushes of sessions VibeSynergy now delivers itself.

    With its push off they stay on Garmin under the same name as ours. On a day with a
    delivered session, a scheduled workout with that name that the ledger does not know as ours
    is such a copy: off the calendar and out of the library. Days without a delivery, and
    workouts with any other name (the athlete's own), are left alone.
    """
    names: dict[date, set[str]] = {}
    for want in wanted.values():
        name = str(want["workout"].get("workoutName") or "").strip().casefold()
        if name:
            names.setdefault(want["date"], set()).add(name)
    ours_scheduled, ours_workouts = set(), set()
    for row in _deliveries(engine).values():
        if row.get("scheduled_id"):
            ours_scheduled.add(int(row["scheduled_id"]))
        if row.get("workout_id"):
            ours_workouts.add(int(row["workout_id"]))
    removed = 0
    for day, wanted_names in sorted(names.items()):
        for item in _garmin_day(engine, day):
            if item["scheduled_id"] in ours_scheduled:
                continue
            if item["title"].strip().casefold() not in wanted_names:
                continue
            if item["workout_id"] in ours_workouts:
                continue
            log.info("Garmin: removing intervals.icu's copy %s (%s)", item["title"], day)
            _unschedule(api, item["scheduled_id"])
            if item["workout_id"]:
                _delete(api, item["workout_id"])
            removed += 1
    return removed


def _id(payload: Any, *keys: str) -> int | None:
    if isinstance(payload, dict):
        for key in keys:
            if (value := _as_int(payload.get(key))) is not None:
                return value
    return None


def _as_int(value: Any) -> int | None:
    text = str(value or "")
    return int(text) if text.isdigit() else None


class _Lazy:
    """Logs in to Garmin only when a run has something to write."""

    def __init__(self, factory):
        self._factory = factory
        self._api = None

    def __getattr__(self, name: str):
        if self._api is None:
            self._api = self._factory()
        return getattr(self._api, name)


def reconcile(
    engine,
    api_factory,
    today: date,
    sports: tuple[str, ...] = (),
    lookahead: int = LOOKAHEAD_DAYS,
) -> dict[str, Any]:
    from vibesynergy.query import garmin_endurance

    api = _Lazy(api_factory)
    wanted = desired(engine, today, lookahead)
    # Endurance sports switched on for direct delivery ride the same machinery.
    wanted.update(garmin_endurance.desired(engine, today, sports, lookahead))
    known = _deliveries(engine)
    result = {"delivered": 0, "withdrawn": 0, "errors": 0}
    calendar_at = _calendar_synced_at(engine)
    for event_id, want in wanted.items():
        row = known.get(event_id) or {}
        try:
            if _went_missing(engine, row, want["date"], calendar_at):
                # Gone from Garmin's calendar (removed there by hand): put it back.
                log.info("Garmin strength: event %s is no longer on Garmin, redelivering", event_id)
                row = {**row, "state": "missing"}
            if row.get("fingerprint") != want["fingerprint"] or row.get("state") != "delivered":
                if row.get("state") in ("delivered", "uploaded", "missing"):
                    _withdraw(api, row)
                    _save(engine, event_id, state="withdrawn", scheduled_id=None, workout_id=None)
                created = api.upload_workout(want["workout"])
                workout_id = _id(created, "workoutId", "workout_id", "id")
                if workout_id is None:
                    raise RuntimeError("Garmin did not return a workout id")
                _save(
                    engine,
                    event_id,
                    event_date=want["date"],
                    fingerprint=want["fingerprint"],
                    workout_id=workout_id,
                    scheduled_id=None,
                    state="uploaded",
                    last_error=None,
                )
                scheduled = api.schedule_workout(workout_id, want["date"].isoformat())
                scheduled_id = _id(scheduled, "workoutScheduleId", "scheduledWorkoutId", "id")
                _save(engine, event_id, scheduled_id=scheduled_id, state="delivered")
                row = {**row, "workout_id": workout_id, "scheduled_id": scheduled_id}
                result["delivered"] += 1
        except Exception as exc:
            log.warning("Garmin strength: event %s not delivered (%s)", event_id, exc)
            _save(engine, event_id, event_date=want["date"], last_error=str(exc)[:500])
            result["errors"] += 1
    try:
        result["copies_removed"] = _drop_copies(engine, api, wanted)
    except Exception as exc:
        log.warning("Garmin strength: intervals.icu copies not removed (%s)", exc)
        result["errors"] += 1
    for event_id, row in known.items():
        if event_id in wanted or row.get("state") not in ("delivered", "uploaded"):
            continue
        if row.get("event_date") and row["event_date"] < today:
            continue  # a past session stays as it was
        try:
            _withdraw(api, row)
            _save(engine, event_id, state="withdrawn", scheduled_id=None, workout_id=None)
            result["withdrawn"] += 1
        except Exception as exc:
            log.warning("Garmin strength: event %s not withdrawn (%s)", event_id, exc)
            _save(engine, event_id, last_error=str(exc)[:500])
            result["errors"] += 1
    return result


def run(rt) -> dict[str, Any]:
    if not _garmin_connected(rt):
        return {"skipped": "not_connected"}
    from vibesynergy.portal import prefs

    today = today_in(rt.settings.timezone)
    sports = tuple(prefs.load(rt.settings.data_dir).get("garmin_delivery") or ())
    # The plan horizon: 9 days by default, 7 to 14 in Settings. Sessions further
    # ahead come onto Garmin as the window moves; ones that fall outside are withdrawn.
    lookahead = prefs.horizon_days(rt.settings.data_dir) - 1
    with SecretStore(rt.settings.secrets_dir, "garmin").lock("calendar-read-write"):
        return reconcile(rt.engine, lambda: _api(rt), today, sports, lookahead)
