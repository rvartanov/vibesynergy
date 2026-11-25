"""Every planned workout is complete on its own (#192).

A session's text must never point at another session for its content: "zelfde drills als
vorige week", "zoals in week 4", "same as last week". The athlete reads one workout on the
watch, and the gear line (#112) only sees what the steps name.

* A text that refers to another session for its content is a warning
  (``incomplete_reference``, plan_lint).
* That reference together with work steps that carry no name beyond an intensity word or
  a zone ("6x50m Licht") is refused: there is nothing to do on the watch.

These are text-quality rules (how a reference is phrased, which words are only an
intensity), not coaching values: no drill, cue or intensity is chosen here.
"""

from __future__ import annotations

import re
from typing import Any

from vibesynergy.query import workout_model

_DAYS_NL = r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|gisteren|eergisteren)"
_DAYS_EN = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|yesterday)"
# A date without a year ("als 10-11"), not a range of seconds, minutes, % or watts.
_DATE = r"(?:op\s+)?\d{1,2}-\d{1,2}(?!\d|\s*(?:%|s\b|sec|min|'|x|\u00d7|w\b|bpm|rpm))"
# The other session a text points at: last week, last time, a weekday, week N, a date.
_OTHER_NL = (
    r"(?:(?:de\s+|het\s+)?(?:vorige|afgelopen)\s+(?:week|keer|sessie|training|les)"
    r"|vorig(?:e)?\s+week|(?:in\s+)?week\s+\d+|"
    + _DATE
    + r"|(?:op\s+|afgelopen\s+)?"
    + _DAYS_NL
    + r")"
)
_OTHER_EN = (
    r"(?:(?:the\s+)?(?:last|previous)\s+(?:week|time|session|workout)"
    r"|(?:in\s+)?week\s+\d+|" + _DATE + r"|(?:on\s+|last\s+)?" + _DAYS_EN + r")"
)
# Short gap: "zelfde drills als", "dezelfde opbouw als", "same set as": within a clause.
_GAP = r"[^.;:\n]{0,40}?"
REFERENCES = (
    re.compile(r"\b(?:de|het)?zelfde\b" + _GAP + r"\bals\s+" + _OTHER_NL + r"\b", re.I),
    re.compile(r"\b(?:net\s+)?zoals\s+(?:in\s+|op\s+)?" + _OTHER_NL + r"\b", re.I),
    re.compile(r"\bherhaling\s+van\s+" + _GAP + _OTHER_NL + r"\b", re.I),
    re.compile(r"\bsame\b" + _GAP + r"\bas\s+" + _OTHER_EN + r"\b", re.I),
    re.compile(r"\bas\s+(?:in|on)\s+" + _OTHER_EN + r"\b", re.I),
    re.compile(r"\blike\s+(?:in\s+|on\s+)?" + _OTHER_EN + r"\b", re.I),
    re.compile(r"\brepeat\s+(?:of\s+)?" + _OTHER_EN + r"\b", re.I),
)

# Words that only say how hard, not what: a step with nothing else in its cue has no name.
_INTENSITY = frozenset(
    [
        "licht",
        "middel",
        "zwaar",
        "rustig",
        "rustige",
        "makkelijk",
        "matig",
        "stevig",
        "hard",
        "voluit",
        "gelijkmatig",
        "easy",
        "moderate",
        "steady",
        "medium",
        "hard",
        "tempo",
        "all-out",
        "even",
        "rpe",
        "z",
        "zone",
        "pace",
    ]
)
_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)
_LEAF_SKIP = {"rest", "recovery"}
_EDGE = {"warmup", "cooldown"}


def references(text: str) -> list[str]:
    """The phrases in ``text`` that point at another session for its content."""
    found = []
    for pattern in REFERENCES:
        found += [m.group(0).strip() for m in pattern.finditer(text or "")]
    return found


def _leaves(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in steps or []:
        if step.get("kind") == "repeat":
            out += _leaves(step.get("steps") or [])
        elif step.get("kind") not in _LEAF_SKIP:
            out.append(step)
    return out


# Zone-only targets say how hard, like an intensity word; a power or %LTHR target or a
# cadence says exactly what the step is on the watch.
_LOOSE_TARGETS = {None, "none", "hr_zone", "pace_zone"}


def named(step: dict[str, Any]) -> bool:
    """Whether a step says what to do: a cue beyond an intensity word, or an exact target."""
    if step.get("cadence") or (step.get("target") or {}).get("type") not in _LOOSE_TARGETS:
        return True
    words = _TOKEN.findall(str(step.get("cue") or "").casefold())
    return any(w not in _INTENSITY and len(w) > 1 for w in words)


def nameless(steps: list[dict[str, Any]]) -> bool:
    """No work step names what to do (only "Licht", "Z2", nothing). Warm-up and cool-down
    count only when the session has no other steps."""
    leaves = _leaves(steps)
    work = [s for s in leaves if s.get("kind") not in _EDGE] or leaves
    return bool(work) and not any(named(s) for s in work)


def cues(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("cue")) for s in _leaves(steps) if s.get("cue")]


def finding(intro: str, steps: list[dict[str, Any]], extra: str = "") -> dict[str, Any] | None:
    """``{"level", "phrase"}`` when the session refers to another one for its content:
    "error" when its work steps carry no names either, else "warning"."""
    text = "\n".join([intro or "", extra or "", *cues(steps)])
    found = references(text)
    if not found:
        return None
    return {"level": "error" if nameless(steps) else "warning", "phrase": found[0]}


def refuse(description: str) -> None:
    """Raise ``ValueError`` (for the AI coach, who reads it and writes again) when a session
    refers to another one and its steps name nothing."""
    parsed = workout_model.parse_intervals(description or "")
    found = finding(parsed.intro, parsed.steps, parsed.garmin_block or "")
    if found and found["level"] == "error":
        raise ValueError(
            f"incomplete_reference: the text refers to another session ({found['phrase']!r}) "
            "and the steps name no drill or exercise, only an intensity. Every session is "
            "complete on its own: write each drill and step out by name with its cue, and "
            "do not refer to another session for its content."
        )
