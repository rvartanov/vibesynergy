"""Mechanical fixes for the knowledge-base lint (#137).

``kb_lint`` finds what is wrong in a text; this module says which of those findings have
one replacement that needs no coaching judgment, and checks whether a changed text is the
old text with only such replacements in it.

A **slot** is a place in the old text with a lint finding and the replacements that fix it
mechanically:

- ``retired_name``: an old article slug becomes the current slug (``knowledge.SLUG_ALIASES``),
  the old app name "Vitality" becomes "VibeSynergy", and a ``vitality_<name>`` tool becomes
  ``vibesynergy_<name>`` when that tool exists. The removed plan-source command and the
  retired loop name have no such replacement: what to write instead is a decision.
- ``unknown_tool``: a renamed tool in ``TOOL_RENAMES`` becomes its new name. A tool of
  another app (``SendUserMessage``) has no one-to-one replacement.
- ``rule_range``: a range from 1 (the whole family) gets the real end: ``ICU-1..4`` becomes
  ``ICU-1..6`` while ICU-6 is the highest rule in use. A partial range, or a single rule
  that does not exist, is a decision.
- ``fixed_threshold``: in a text the AI coach follows as an instruction, a threshold value
  becomes "read live with vibesynergy_thresholds" (Dutch: "lees live met ..."), but only on
  a line that is clearly an instruction: not a table, a heading or code, no date on the line
  or in its section heading (a dated record of what was agreed), no example, and only one
  threshold value on the line (several values are a zone listing).

``explain`` is the strict part: the new text must be the old text, character for character,
except at slots, where it holds one of the slot's replacements. Anything else, anywhere, is
a content change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache

from vibesynergy.query import kb_lint

# Renamed VibeSynergy tools: old name -> the tool that replaced it. Add an entry when a tool
# is renamed, so texts that still name the old one are fixed without a proposal.
TOOL_RENAMES: dict[str, str] = {}

LIVE = {
    "nl": f"lees live met {kb_lint.THRESHOLD_TOOL}",
    "en": f"read live with {kb_lint.THRESHOLD_TOOL}",
}
# The checks a slot can fix, in the order the portal names them.
FIXABLE = ("retired_name", "unknown_tool", "rule_range", "fixed_threshold")


@dataclass(frozen=True)
class Slot:
    start: int
    end: int
    check: str
    found: str
    options: tuple[str, ...]  # the replacements; the original text is always allowed too


@dataclass(frozen=True)
class Fix:
    check: str
    found: str
    fixed: str

    def as_dict(self) -> dict[str, str]:
        return {"check": self.check, "found": self.found, "fixed": self.fixed}


def normal(text: str | None) -> str:
    """The form texts are compared in: line ends as ``\\n``, no outer whitespace (the
    knowledge base strips every text it stores)."""
    return (text or "").replace("\r\n", "\n").strip()


# --- slots ---------------------------------------------------------------------------------

_APP = re.compile(r"(?<![A-Za-z0-9])Vitality(?![A-Za-z0-9_])")
_APP_TOOL = re.compile(r"(?<![A-Za-z0-9_])vitality_([a-z0-9_]+)")
_PLAN_SOURCE = re.compile(r"(?<![A-Za-z])(?:vitality|vibesynergy)\s+plan\s+source\b", re.I)
_SHERPA = re.compile(r"(?<![A-Za-z0-9])Sherpa(?![A-Za-z0-9])", re.I)
_VS_TOOL = re.compile(r"(?<![A-Za-z0-9_])vibesynergy_[a-z0-9_]+(?![a-z0-9_*])")


def slots(
    text: str,
    *,
    slug_aliases: Mapping[str, str],
    families: Mapping[str, set[int]],
    tools: frozenset[str],
    thresholds: bool,
) -> list[Slot]:
    """Every place in ``text`` with a mechanical fix, in order, never overlapping."""
    found: list[Slot] = []
    # Findings without a mechanical fix: a slot inside one is no fix either (the "vitality"
    # in "vitality plan source" is part of a removed command, not the app name).
    blocked = [m.span() for m in _PLAN_SOURCE.finditer(text)]
    blocked += [m.span() for m in _SHERPA.finditer(text)]
    for old, new in slug_aliases.items():
        pattern = re.compile(r"(?<![a-z0-9-])" + re.escape(old) + r"(?![a-z0-9-])")
        found += [Slot(*m.span(), "retired_name", old, (new,)) for m in pattern.finditer(text)]
    for m in _APP_TOOL.finditer(text):
        tool = f"vibesynergy_{m.group(1).rstrip('_')}"
        if tool in tools:
            found.append(
                Slot(
                    m.start(),
                    m.start() + len("vitality_") + len(m.group(1).rstrip("_")),
                    "retired_name",
                    m.group(0).rstrip("_"),
                    (tool,),
                )
            )
    found += [
        Slot(*m.span(), "retired_name", "Vitality", ("VibeSynergy",)) for m in _APP.finditer(text)
    ]
    for m in _VS_TOOL.finditer(text):
        name = m.group(0)
        if name not in tools and TOOL_RENAMES.get(name) in tools:
            found.append(Slot(*m.span(), "unknown_tool", name, (TOOL_RENAMES[name],)))
    found += _range_slots(text, families)
    if thresholds:
        found += _threshold_slots(text)
    found.sort(key=lambda s: (s.start, s.end))
    out: list[Slot] = []
    for slot in found:
        if any(a < slot.end and slot.start < b for a, b in blocked):
            continue
        if out and slot.start < out[-1].end:
            continue  # overlapping findings: the first one wins, the other stays a warning
        out.append(slot)
    return out


def _range_slots(text: str, families: Mapping[str, set[int]]) -> list[Slot]:
    out = []
    for m in kb_lint._RULE_REF.finditer(text):
        prefix, start, end = m.group(1), int(m.group(2)), m.group(3)
        numbers = families.get(prefix)
        if end is None or not numbers or start != 1 or int(end) == max(numbers):
            continue
        whole = m.group(0)
        cut = m.start(3) - m.start()
        same_style = whole[:cut] + str(max(numbers)) + whole[cut + len(end) :]
        options = tuple(dict.fromkeys((same_style, f"{prefix}-1..{max(numbers)}")))
        out.append(Slot(*m.span(), "rule_range", f"{prefix}-{start}..{end}", options))
    return out


# A threshold named by a label keeps the label: "FTP 265 W" -> "FTP (read live with ...)".
_LABEL = re.compile(
    r"^(?:FTP|LTHR|CSS|HRmax|drempeltempo|threshold pace|Z\d)\b[^\d]*?(?=\d)", re.IGNORECASE
)
_DATE = re.compile(
    r"(?<![\d.,%:/-])(?:\d{4}-\d{2}-\d{2}|(?:[12]\d|3[01]|0?[1-9])-(?:1[0-2]|0?[1-9])(?:-\d{4})?)"
    r"(?![\d%:/-]|\s?(?:W|bpm|%|km|m\b|s\b))"
)
_EXAMPLE = re.compile(r"\b(?:voorbeeld|bijv|bv|example|e\.g|for instance)\b", re.IGNORECASE)
_LANG_NL = re.compile(r"\b(?:de|het|een|en|van|met|niet|voor|je|wordt|naar)\b", re.IGNORECASE)
_LANG_EN = re.compile(r"\b(?:the|a|and|of|with|not|for|you|is|to)\b", re.IGNORECASE)


def language(text: str) -> str:
    """``nl`` or ``en``: the language of the phrase a threshold is replaced with."""
    return "nl" if len(_LANG_NL.findall(text)) >= len(_LANG_EN.findall(text)) else "en"


def _instruction_lines(text: str) -> Iterable[tuple[int, str]]:
    """(offset, line) for the lines that are clearly an instruction (see the module doc)."""
    offset = 0
    fenced = False
    heading_dated = False
    for line in text.split("\n"):
        start, offset = offset, offset + len(line) + 1
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if stripped.startswith("#"):
            heading_dated = bool(_DATE.search(stripped))
            continue
        if fenced or heading_dated or stripped.startswith("|") or not stripped:
            continue
        if _DATE.search(line) or _EXAMPLE.search(line):
            continue
        yield start, line


def _threshold_slots(text: str) -> list[Slot]:
    phrases = (LIVE[language(text)], *(p for k, p in LIVE.items() if k != language(text)))
    out = []
    for start, line in _instruction_lines(text):
        matches = list(kb_lint._THRESHOLD.finditer(line))
        if len(matches) != 1:
            continue
        m = matches[0]
        whole = m.group(0)
        if "`" in line[: m.start()] and "`" in line[m.end() :]:
            continue  # inside code
        label = _LABEL.match(whole)
        options: list[str] = []
        for phrase in phrases:
            if label:
                options.append(f"{label.group(0).rstrip()} ({phrase})")
            else:
                options += [f"({phrase})", phrase]
        found = re.sub(r"\s+", " ", whole.replace("*", "")).strip()
        out.append(
            Slot(start + m.start(), start + m.end(), "fixed_threshold", found, tuple(options))
        )
    return out


# --- applying and explaining ----------------------------------------------------------------


def autofix(text: str, found: list[Slot]) -> tuple[str, list[Fix]]:
    """The text with every slot replaced by its first replacement."""
    parts: list[str] = []
    fixes: list[Fix] = []
    pos = 0
    for slot in found:
        parts += [text[pos : slot.start], slot.options[0]]
        fixes.append(Fix(slot.check, slot.found, slot.options[0]))
        pos = slot.end
    parts.append(text[pos:])
    return "".join(parts), fixes


def explain(old: str, new: str, found: list[Slot]) -> list[Fix] | None:
    """The fixes that turn ``old`` into ``new``, or ``None`` when ``new`` differs from
    ``old`` anywhere outside a slot, or holds something at a slot that is not one of its
    replacements. An unchanged text gives ``[]``."""
    literals = []
    pos = 0
    for slot in found:
        literals.append(old[pos : slot.start])
        pos = slot.end
    tail = old[pos:]

    @cache
    def walk(index: int, at: int) -> tuple[Fix, ...] | None:
        if index == len(found):
            return () if new[at:] == tail else None
        literal = literals[index]
        if not new.startswith(literal, at):
            return None
        at += len(literal)
        slot = found[index]
        original = old[slot.start : slot.end]
        for choice in (original, *slot.options):
            if new.startswith(choice, at):
                rest = walk(index + 1, at + len(choice))
                if rest is not None:
                    if choice == original:
                        return rest
                    return (Fix(slot.check, slot.found, choice), *rest)
        return None

    result = walk(0, 0)
    return None if result is None else list(result)
