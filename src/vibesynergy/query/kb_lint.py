"""Text checks behind the knowledge-base lint (``knowledge.lint``, #96 and #120).

Each check takes one text and returns what it found, in order of appearance, each once.
``knowledge.lint`` decides which articles and rules a check reads and turns a finding into
a warning. A warning only: the athlete decides about the text, through a proposal.

- ``retired_names``: names that are gone (the old app name, the plan-source command, old
  article slugs). VibeSynergy is the app and the only plan source (#111).
- ``rule_ranges`` and ``unknown_rules``: a reference such as ``ICU-1..4`` while ICU-6 is in
  use, or ``TRI-9`` that does not exist.
- ``unknown_tools``: a ``vibesynergy_*`` tool the MCP server does not register, or a tool of
  another app (``SendUserMessage``).
- ``fixed_thresholds``: a threshold written down as a number (300 W, LTHR 170, 5:00/km).
  Thresholds change after every test; the AI coach reads them live with
  vibesynergy_thresholds.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

THRESHOLD_TOOL = "vibesynergy_thresholds"


@dataclass(frozen=True)
class Retired:
    name: str
    pattern: re.Pattern[str]
    hint: str


def _word(text: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(text) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def retired_list(slug_aliases: Mapping[str, str]) -> tuple[Retired, ...]:
    """The retired names. The old slugs come from ``knowledge.SLUG_ALIASES``."""
    fixed = (
        Retired("Vitality", _word("Vitality"), "the app is VibeSynergy"),
        Retired(
            "plan source",
            re.compile(r"(?<![A-Za-z])(?:vitality|vibesynergy)\s+plan\s+source\b", re.I),
            "the command is gone: VibeSynergy is the only plan source",
        ),
        Retired("Sherpa", _word("Sherpa"), "that loop is retired; ai-coach-loop is the method"),
    )
    slugs = tuple(
        Retired(
            old,
            re.compile(r"(?<![a-z0-9-])" + re.escape(old) + r"(?![a-z0-9-])", re.I),
            f"the article is {new} now",
        )
        for old, new in slug_aliases.items()
    )
    return fixed + slugs


def retired_names(text: str, retired: Iterable[Retired]) -> list[tuple[str, str]]:
    """(name, hint) for every retired name in the text, in order of appearance."""
    hits: list[tuple[int, str, str]] = []
    for item in retired:
        match = item.pattern.search(text)
        if match:
            hits.append((match.start(), item.name, item.hint))
    return [(name, hint) for _pos, name, hint in sorted(hits)]


# --- rule references -----------------------------------------------------------------------

_RULE_ID = re.compile(r"^([A-Z][A-Z0-9]*)-(\d+)$")
# TRI-1..8, ICU-1 to 6 (also with a dash), HRV-1 t/m 5, or one rule (TRI-9).
# Not ``HRV-*`` (a whole family).
_RULE_REF = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9]{1,9})-(\d{1,3})"
    r"(?:\s*(?:\.\.|\u2026|\u2013|\u2014|t/m|to)\s*(?:\1-)?(\d{1,3}))?(?![\d])"
)


def rule_families(rule_ids: Iterable[str]) -> dict[str, set[int]]:
    """Rule numbers in use per family: ``{"ICU": {1, ..., 6}}``."""
    families: dict[str, set[int]] = {}
    for rule_id in rule_ids:
        match = _RULE_ID.match(rule_id)
        if match:
            families.setdefault(match.group(1), set()).add(int(match.group(2)))
    return families


def _span(prefix: str, numbers: set[int]) -> str:
    return f"{prefix}-{min(numbers)}..{max(numbers)}"


def rule_ranges(text: str, families: Mapping[str, set[int]]) -> list[tuple[str, str]]:
    """(found, expected) for every range that does not match the rules in use.

    A range from 1 means the whole family, so its end must be the highest rule in use
    (``ICU-1..4`` while ICU-6 exists). A range from elsewhere is a deliberate part; it only
    warns when it names a rule that does not exist."""
    out: list[tuple[str, str]] = []
    for match in _RULE_REF.finditer(text):
        prefix, start, end = match.group(1), int(match.group(2)), match.group(3)
        numbers = families.get(prefix)
        if end is None or not numbers:
            continue
        end_n = int(end)
        found = f"{prefix}-{start}..{end_n}"
        expected = _span(prefix, numbers)
        if start == 1:
            wrong = end_n != max(numbers)
        else:
            wrong = any(n not in numbers for n in range(start, end_n + 1)) or end_n < start
        if wrong and (found, expected) not in out:
            out.append((found, expected))
    return out


def unknown_rules(text: str, families: Mapping[str, set[int]]) -> list[str]:
    """Single rule ids of a family in use whose number is not in use (``TRI-9``)."""
    out: list[str] = []
    for match in _RULE_REF.finditer(text):
        prefix, number, end = match.group(1), int(match.group(2)), match.group(3)
        numbers = families.get(prefix)
        if end is not None or not numbers or number in numbers:
            continue
        ref = f"{prefix}-{number}"
        if ref not in out:
            out.append(ref)
    return out


# --- tool names ----------------------------------------------------------------------------

_VS_TOOL = re.compile(r"(?<![A-Za-z0-9_])vibesynergy_[a-z0-9_]+\*?")
# Tools of other apps that a copied routine may still name. The API runner rewrites them
# (coach_runner/prompts.py), but an AI app over MCP reads the text as it is.
FOREIGN_TOOLS = {
    "SendUserMessage": "the summary of your vibesynergy_coach_write is the report VibeSynergy "
    "sends",
    "ToolSearch": "every VibeSynergy tool is listed already",
}
_FOREIGN = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(FOREIGN_TOOLS) + r")(?![A-Za-z0-9_])")


def unknown_tools(text: str, known: frozenset[str]) -> list[str]:
    """Tool names in the text that the MCP server does not register, in order.
    ``vibesynergy_plan_*`` stands for a group and counts when one tool starts with it."""
    hits: list[tuple[int, str]] = []
    for match in _VS_TOOL.finditer(text):
        name = match.group(0)
        if name.endswith("*"):
            prefix = name.rstrip("*")
            ok = any(tool.startswith(prefix) for tool in known)
        else:
            name = name.rstrip("_")
            ok = name in known
        if not ok:
            hits.append((match.start(), name))
    hits.extend((match.start(), match.group(1)) for match in _FOREIGN.finditer(text))
    out: list[str] = []
    for _pos, name in sorted(hits):
        if name not in out:
            out.append(name)
    return out


# --- fixed thresholds ----------------------------------------------------------------------

_DASH = r"\s?[-\u2013]\s?"
_THRESHOLD = re.compile(
    "|".join(
        (
            # power: 300 W, 250-270 W (not %FTP, which is relative)
            rf"(?<![\d.,])\d{{2,3}}(?:{_DASH}\d{{2,3}})?\s?W(?![A-Za-z])",
            # heart rate with a unit: 140 bpm, 120-130 sl/min
            rf"(?<![\d.,])\d{{2,3}}(?:{_DASH}\d{{2,3}})?\s?(?:bpm|sl/min|slagen/min)(?![A-Za-z])",
            # pace: 5:00/km, 2:00/100 m, 1:45 min/100m
            r"(?<![\d:])\d{1,2}:\d{2}(?:\s?min)?\s?/\s?(?:km|mi|100\s?m)(?![A-Za-z])",
            # a threshold named with a bare value: FTP 300, LTHR lopen 170 / fiets 140,
            # CSS 120 (three digits, so "FTP 20'" and "wk 27" are no thresholds)
            r"(?:FTP|LTHR|CSS|HRmax|drempeltempo|threshold pace)\b[^.\n;:()]{0,20}?"
            # (not "CSS-test 400/200": distances of a test, written with a slash)
            r"(?<![\d.,/])\d{3}(?:\s?W(?![A-Za-z]))?(?![\d%'\u2019/])",
            # a heart-rate zone with bare bounds: Z2 120-130
            rf"(?<![A-Za-z])Z\d\s\d{{2,3}}{_DASH}\d{{2,3}}(?![\d%])",
        )
    ),
    re.IGNORECASE,
)


def fixed_thresholds(text: str) -> list[str]:
    """Threshold values written down as numbers, as they appear (whitespace squeezed)."""
    out: list[str] = []
    for match in _THRESHOLD.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0).replace("*", "")).strip()
        if value not in out:
            out.append(value)
    return out
