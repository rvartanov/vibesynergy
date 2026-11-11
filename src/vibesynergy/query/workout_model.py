"""One workout model for the plan and Garmin delivery.

A workout is a list of steps as data, always with relative targets (ICU-6): % of FTP,
% of LTHR, a heart-rate or pace zone, or none. ``parse_intervals`` reads the intervals.icu
step syntax that the plan uses today; ``to_intervals`` writes it back. Text outside the
steps (the cue for the athlete) and a ``[garmin]`` strength block stay as they are and are
returned separately.

Step kinds: warmup, work, rest, recovery, cooldown, repeat. A step ends on ``duration_s``,
``distance_m`` or ``until_lap`` (``Press lap``; the duration then only counts for the
estimate). The semantic round trip ``parse(to_intervals(parse(x))) == parse(x)`` holds for
every workout in the live plan (tests use anonymised copies).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SECTIONS = {
    "warmup": "warmup",
    "warm up": "warmup",
    "main set": "work",
    "main": "work",
    "cooldown": "cooldown",
    "cool down": "cooldown",
}
_REPEAT = re.compile(r"^(?P<head>[A-Za-z][A-Za-z ]*?)?\s*(?P<n>\d+)x\s*$")
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_DISTANCE = re.compile(r"^(\d+(?:[.,]\d+)?)(mtr|km)$")
_PCT = re.compile(r"^(\d+)(?:-(\d+))?%$")
_ZONE = re.compile(r"^Z(\d)(?:-Z(\d))?$")
_RPM = re.compile(r"^(\d+)(?:-(\d+))?rpm$")
_INTENSITY = {
    "warmup": "warmup",
    "cooldown": "cooldown",
    "rest": "rest",
    "recovery": "recovery",
    "interval": "work",
    "active": "work",
}


@dataclass
class Workout:
    steps: list[dict[str, Any]] = field(default_factory=list)
    intro: str = ""  # cue text before the first step
    garmin_block: str = ""  # the [garmin] lines, kept verbatim (garmin_strength parses them)
    unparsed: list[str] = field(default_factory=list)


def _duration(token: str) -> int | None:
    match = _DURATION.match(token)
    if not match or not any(match.groups()):
        return None
    h, m, s = (int(g) if g else 0 for g in match.groups())
    return h * 3600 + m * 60 + s


def _distance(token: str) -> float | None:
    match = _DISTANCE.match(token)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * 1000 if match.group(2) == "km" else value


def parse_step(line: str, section: str | None) -> dict[str, Any] | None:
    """One ``- …`` line. None when it is not a step this model knows."""
    body = line.strip()[1:].strip()
    until_lap = False
    if body.lower().startswith("press lap"):
        until_lap, body = True, body[len("press lap") :].strip()
    tokens = body.split()
    if "press" in [t.lower() for t in tokens]:
        i = [t.lower() for t in tokens].index("press")
        if i + 1 < len(tokens) and tokens[i + 1].lower() == "lap":
            until_lap = True
            del tokens[i : i + 2]
    cue: list[str] = []
    step: dict[str, Any] = {"kind": section or "work"}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        seconds, metres = _duration(token), _distance(token)
        if seconds is not None or metres is not None:
            if seconds is not None:
                step["duration_s"] = seconds
            else:
                step["distance_m"] = metres
            i += 1
            break
        cue.append(token)
        i += 1
    else:
        return None  # no duration or distance: not a step
    rest = tokens[i:]
    target: dict[str, Any] | None = None
    j = 0
    while j < len(rest):
        token = rest[j]
        nxt = rest[j + 1] if j + 1 < len(rest) else ""
        if token.startswith("intensity="):
            kind = _INTENSITY.get(token.split("=", 1)[1].lower())
            if kind is None:
                return None
            step["kind"] = kind
        elif token == "free":
            target = {"type": "none"}
        elif token == "rest":
            step["kind"] = "rest"  # intervals.icu drops it as text; the step is rest anyway
        elif (m := _PCT.match(token)) is not None:
            low = int(m.group(1))
            high = int(m.group(2) or m.group(1))
            if nxt.upper() == "LTHR":
                target, j = {"type": "pct_lthr", "low": low, "high": high}, j + 1
            else:
                target = {"type": "pct_ftp", "low": low, "high": high}
        elif (m := _ZONE.match(token)) is not None and nxt in ("HR", "Pace"):
            low = int(m.group(1))
            high = int(m.group(2) or m.group(1))
            kind = "hr_zone" if nxt == "HR" else "pace_zone"
            target, j = {"type": kind, "low": low, "high": high}, j + 1
        elif (m := _RPM.match(token)) is not None:
            low = int(m.group(1))
            step["cadence"] = [low, int(m.group(2) or low)]
        else:
            return None
        j += 1
    if target:
        step["target"] = target
    if cue:
        step["cue"] = " ".join(cue)
    if until_lap:
        step["until_lap"] = True
    return step


def parse_intervals(description: str | None) -> Workout:
    out = Workout()
    lines = (description or "").splitlines()
    section: str | None = None
    repeat: dict[str, Any] | None = None
    intro: list[str] = []
    in_block = False
    garmin: list[str] = []
    started = False
    for raw in lines:
        line = raw.rstrip()
        text = line.strip()
        if text == "[garmin]":
            in_block = True
            garmin.append(text)
            continue
        if in_block:
            if text:
                garmin.append(text)
                continue
            in_block = False
        if not text:
            repeat = None
            continue
        if text.startswith("- "):
            started = True
            step = parse_step(text, section)
            if step is None:
                out.unparsed.append(text)
                continue
            (repeat["steps"] if repeat is not None else out.steps).append(step)
            continue
        head = text.lower()
        if head in _SECTIONS:
            started, section, repeat = True, _SECTIONS[head], None
            continue
        if (m := _REPEAT.match(text)) is not None:
            started = True
            if m.group("head") and m.group("head").strip().lower() in _SECTIONS:
                section = _SECTIONS[m.group("head").strip().lower()]
            repeat = {"kind": "repeat", "times": int(m.group("n")), "steps": []}
            out.steps.append(repeat)
            continue
        if not started:
            intro.append(line)
        else:
            out.unparsed.append(text)
    out.intro = "\n".join(intro).strip()
    out.garmin_block = "\n".join(garmin)
    return out


_SECTION_HEAD = {"warmup": "Warmup", "work": "Main set", "cooldown": "Cooldown"}


def _step_text(step: dict[str, Any]) -> str:
    parts: list[str] = ["-"]
    if step.get("until_lap"):
        parts.append("Press lap")
    if step.get("cue"):
        parts.append(step["cue"])
    if "distance_m" in step:
        d = step["distance_m"]
        parts.append(f"{d / 1000:g}km" if d >= 1000 and d % 1000 else f"{int(d)}mtr")
    else:
        s = int(step.get("duration_s") or 0)
        h, m, sec = s // 3600, s % 3600 // 60, s % 60
        parts.append(f"{h}h" * bool(h) + f"{m}m" * bool(m) + f"{sec}s" * bool(sec) or "0s")
    target = step.get("target") or {}
    kind = target.get("type")
    rng = (
        f"{target.get('low')}"
        if target.get("low") == target.get("high")
        else f"{target.get('low')}-{target.get('high')}"
    )
    if kind == "pct_ftp":
        parts.append(f"{rng}%")
    elif kind == "pct_lthr":
        parts.append(f"{rng}% LTHR")
    elif kind in ("hr_zone", "pace_zone"):
        zone = (
            f"Z{target['low']}"
            if target["low"] == target["high"]
            else f"Z{target['low']}-Z{target['high']}"
        )
        parts.append(f"{zone} {'HR' if kind == 'hr_zone' else 'Pace'}")
    elif kind == "none":
        parts.append("free")
    if step.get("cadence"):
        low, high = step["cadence"]
        parts.append(f"{low}rpm" if low == high else f"{low}-{high}rpm")
    if step["kind"] in ("rest", "recovery"):
        parts.append(f"intensity={step['kind']}")
    return " ".join(parts)


def total_seconds(steps: list[dict[str, Any]]) -> int:
    """Planned time of the steps (repeats counted); distance-only steps count as 0."""
    total = 0
    for step in steps:
        if step.get("kind") == "repeat":
            total += int(step.get("times") or 1) * total_seconds(step.get("steps") or [])
        else:
            total += int(step.get("duration_s") or 0)
    return total


def to_intervals(workout: Workout) -> str:
    """intervals.icu text for the workout: intro, sections, repeats, then the [garmin] block."""
    out: list[str] = []
    if workout.intro:
        out += [workout.intro, ""]
    section = None
    for step in workout.steps:
        first = step["steps"][0] if step["kind"] == "repeat" and step["steps"] else step
        kind = first["kind"]
        if kind in _SECTION_HEAD and kind != section:
            if out and out[-1] != "":
                out.append("")
            out.append(_SECTION_HEAD[kind])
            section = kind
        if step["kind"] == "repeat":
            if out and out[-1] not in ("", _SECTION_HEAD.get(section or "", "")):
                out.append("")
            out.append(f"{step['times']}x")
            out += [_step_text(s) for s in step["steps"]]
            out.append("")
        else:
            out.append(_step_text(step))
    text = "\n".join(out).strip()
    if workout.garmin_block:
        text = f"{text}\n\n{workout.garmin_block}" if text else workout.garmin_block
    return text
