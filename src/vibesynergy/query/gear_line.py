"""The gear a planned workout needs, as one line at the top of its notes (#112).

"Materiaal: pullbuoy, paddles" (nl) or "Gear: pull buoy, paddles" (en). The line is part of
the session's own text (the intro of its intervals.icu text), so it goes wherever the
description already goes: the start page, This week, the Garmin workout description, the
intervals.icu mirror and the calendar item. No new field.

Only the **words** for gear are here, so VibeSynergy can recognise them in a step's text;
which drill needs which gear is the knowledge base's (``swim_drills``, the workout method).
The labels go through the portal's catalogue (``i18n``), nl and en.

``add`` is idempotent: a text that already has a gear line (in either language) is left as
it is, so re-generating, re-syncing or editing a session never doubles the line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from vibesynergy import i18n

# (English label, the words that name it, the sport families it counts in; None = any).
# "pull" alone and "plank" are only gear in a swim: "pull" is also part of a stroke cue
# elsewhere and "plank" is the strength exercise. "band" (ankle band) and "trainer"/"trail
# shoes" are bound to their sport for the same reason.
VOCABULARY: tuple[tuple[str, str, frozenset[str] | None], ...] = (
    ("pull buoy", r"pull[\s-]?buoy|pullbuoy|buoy|boei", None),
    ("pull buoy", r"pull", frozenset({"swim"})),
    ("paddles", r"paddles?", None),
    ("fins", r"fins|vinnen|zoomers?", None),
    ("snorkel", r"snorkel", None),
    ("kickboard", r"kick[\s-]?board|kickplank|zwemplank", None),
    ("kickboard", r"plank", frozenset({"swim"})),
    ("band", r"band|enkelband", frozenset({"swim"})),
    ("wetsuit", r"wetsuit", None),
    ("indoor trainer", r"(?:indoor\s|fiets)?trainer", frozenset({"ride"})),
    ("trail shoes", r"trail[\s-]?shoes?|trailschoenen", frozenset({"run"})),
)
_WORDS = [
    (label, re.compile(rf"(?<![\w-])(?:{words})(?![\w-])", re.IGNORECASE), sports)
    for label, words, sports in VOCABULARY
]
# The line's label in both languages, so a line in either one is recognised.
_LABEL = "Gear: {items}"
_LINE = re.compile(
    r"^\s*(?:{})\s*:".format(
        "|".join(re.escape(i18n.translator(lang)(_LABEL).split(":")[0]) for lang in i18n.LANGUAGES)
    ),
    re.IGNORECASE | re.MULTILINE,
)


def _family(sport: str | None) -> str:
    s = (sport or "").lower()
    if "swim" in s or "zwem" in s:
        return "swim"
    if "ride" in s or "bike" in s or "cycl" in s or "fiets" in s:
        return "ride"
    if "run" in s or "loop" in s:
        return "run"
    return s


def _texts(steps: Iterable[dict[str, Any]]) -> Iterable[str]:
    for step in steps:
        if step.get("kind") == "repeat":
            yield from _texts(step.get("steps") or [])
        elif step.get("cue"):
            yield str(step["cue"])


def found(steps: Iterable[dict[str, Any]], sport: str | None) -> list[str]:
    """The gear the steps' text names, in the order of ``VOCABULARY``, each once."""
    family = _family(sport)
    text = "\n".join(_texts(steps))
    return list(
        dict.fromkeys(
            label
            for label, pattern, sports in _WORDS
            if (sports is None or family in sports) and pattern.search(text)
        )
    )


def has_line(text: str | None) -> bool:
    """Whether the text already has a gear line (nl or en)."""
    return bool(_LINE.search(text or ""))


def line(gear: list[str], lang: str = "nl") -> str:
    """The gear line in ``lang``: "Materiaal: pullbuoy, paddles"."""
    tr = i18n.translator(lang)
    return tr(_LABEL, items=", ".join(tr(item) for item in gear))


def add(
    intro: str | None, steps: Iterable[dict[str, Any]], sport: str | None, lang: str = "nl"
) -> str:
    """``intro`` with the gear line at the top when the steps name gear and no gear line is
    there yet; otherwise ``intro`` unchanged."""
    intro = intro or ""
    if has_line(intro):
        return intro
    gear = found(steps, sport)
    if not gear:
        return intro
    return f"{line(gear, lang)}\n{intro}".rstrip("\n") if intro else line(gear, lang)
