"""Versions and "What changed" (#140, the model of RunSynergy's releases.py).

- The version starts at ``0.1.0-beta.0.1``; every release adds one to the last number. A next
  minor (``0.2.0-beta.0.1``) only when Robert says so; out of beta it becomes ``1.0.0``.
- Every deploy that ships merged pull requests is a release. ``vibesynergy update`` makes it
  (``vibesynergy.cut_release``): it adds the next ``Release`` at the top of ``RELEASES`` with
  the deploy date and the items nobody released yet, renames ``## Unreleased`` in
  CHANGELOG.md, commits that to ``main``, tags it ``v<version>`` and installs it. Several
  pull requests deployed at once make one release.
- A pull request that noticeably changes use adds its items at the end of ``ITEMS``: short,
  plain language (the communication rule), each for one audience: ``you`` (the person using
  the portal) or ``ai`` (AI coaches and AI apps, for MCP and harness changes). The English
  text is the key; the Dutch goes in ``portal/static/i18n/nl.json``. Small fixes add no
  items; their release gets a number only.
- The portal shows the version in the footer, the items for ``you`` once in a "What changed
  in version …" card after a release, and all of them under Waiting for you → Notices.
  "Seen" is kept per account (``release_seen``). The items for ``ai`` go into the MCP server
  information and ``vibesynergy_coach_handbook`` (``what_changed``).

``pyproject.toml`` keeps a valid PEP 440 version for the line (``0.1.0b0`` for
``0.1.0-beta.0.N``), because the number shown here is not one.

This module imports nothing from VibeSynergy: the release step loads the copy in a fresh
checkout of ``main`` on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

AUDIENCES = ("you", "ai")


@dataclass(frozen=True)
class Item:
    id: str  # stable and unique: the issue or pull request number and a word, "140-version"
    audience: str  # one of AUDIENCES
    text: str  # English; the Dutch is in portal/static/i18n/nl.json


@dataclass(frozen=True)
class Release:
    version: str
    day: date
    items: tuple[str, ...] = ()  # ids from ITEMS; none for a release of small fixes


# Newest first. Written by `vibesynergy update`, not by hand.
RELEASES: tuple[Release, ...] = (
    # The next release goes below this line.
    Release("0.1.0-beta.0.1", date(2026, 11, 22), ("140-version", "140-ai-version")),
)

# What changed, oldest first. A pull request adds its items at the end; the next release
# takes every item no release has yet.
ITEMS: tuple[Item, ...] = (
    Item(
        "140-version",
        "you",
        "VibeSynergy now has a version number, at the bottom of every page. After an update "
        "you see once what changed; after that you find it under Waiting for you → Notices.",
    ),
    Item(
        "140-ai-version",
        "ai",
        "VibeSynergy now has a version number (0.1.0-beta.0.1, then .2 and so on with every "
        "update). What changed for AI apps is in the handbook (what_changed) and in the "
        "server information.",
    ),
    Item(
        "91-races",
        "you",
        "Add, change or delete your races and other events (a camp, a holiday, a time you "
        "can't train) on Planning & progress. VibeSynergy keeps them and copies them to "
        "intervals.icu.",
    ),
    Item(
        "91-ai-races",
        "ai",
        "Races and events have their own tools: vibesynergy_race_add, vibesynergy_event_add, "
        "vibesynergy_race_update, vibesynergy_race_delete, vibesynergy_race_revert and "
        "vibesynergy_races. Each write needs a reason. The athlete's own agent writes at "
        "once; an AI coach's own race change is a proposal unless it has his approval_id.",
    ),
)

VERSION = RELEASES[0].version
FIRST = "0.1.0-beta.0.1"
DASH = "\u2013"  # the en dash of the CHANGELOG headings
MARKER = "    # The next release goes below this line.\n"
_BETA = re.compile(r"^(\d+)\.(\d+)\.(\d+)-beta\.(\d+)\.(\d+)$")
_FINAL = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def version_key(version: str) -> tuple[int, ...]:
    """Sort order: 0.1.0-beta.0.1 < 0.1.0-beta.0.2 < 0.1.0-beta.1.1 < 0.1.0 < 0.1.1."""
    if m := _BETA.match(version):
        major, minor, patch, line, number = map(int, m.groups())
        return (major, minor, patch, 0, line, number)
    if m := _FINAL.match(version):
        major, minor, patch = map(int, m.groups())
        return (major, minor, patch, 1, 0, 0)
    raise ValueError(f"not a VibeSynergy version: {version!r}")


def next_version(version: str | None) -> str:
    """The number after ``version``: one more on the last number (a beta's count, or the
    patch of a version out of beta)."""
    if not version:
        return FIRST
    if m := _BETA.match(version):
        *head, number = m.groups()
        return f"{head[0]}.{head[1]}.{head[2]}-beta.{head[3]}.{int(number) + 1}"
    if m := _FINAL.match(version):
        major, minor, patch = m.groups()
        return f"{major}.{minor}.{int(patch) + 1}"
    raise ValueError(f"not a VibeSynergy version: {version!r}")


def pep440(version: str) -> str:
    """The package version for a line: ``0.1.0-beta.0.N`` → ``0.1.0b0``, ``1.0.3`` → ``1.0.3``.
    pyproject.toml keeps this one, so it changes only with the minor or the beta line."""
    if m := _BETA.match(version):
        major, minor, patch, line, _ = m.groups()
        return f"{major}.{minor}.{patch}b{line}"
    if _FINAL.match(version):
        return version
    raise ValueError(f"not a VibeSynergy version: {version!r}")


def items_of(release: Release, audience: str | None = None) -> list[Item]:
    by_id = {item.id: item for item in ITEMS}
    found = [by_id[i] for i in release.items if i in by_id]
    return [item for item in found if audience is None or item.audience == audience]


def unreleased() -> list[Item]:
    """Items no release has yet: what the next release will carry."""
    taken = {i for release in RELEASES for i in release.items}
    return [item for item in ITEMS if item.id not in taken]


def history(audience: str | None = None) -> list[tuple[Release, list[Item]]]:
    """Every release with items (for ``audience``), newest first."""
    return [(r, found) for r in RELEASES if (found := items_of(r, audience))]


def unseen(seen: str | None, joined: date | None, audience: str = "you"):
    """Releases with items the account did not see yet: newer than ``seen`` (the last version
    it saw) and made after the account was created (a new account gets no old news)."""
    out = []
    for release, found in history(audience):
        if seen and version_key(release.version) <= version_key(seen):
            continue
        if joined and release.day <= joined:
            continue
        out.append((release, found))
    return out


def as_dict(release: Release, found: list[Item]) -> dict:
    return {
        "version": release.version,
        "date": release.day.isoformat(),
        "items": [{"id": item.id, "audience": item.audience, "text": item.text} for item in found],
    }


# --- making a release (vibesynergy.cut_release runs these on a fresh checkout of main) --------


def release_source(version: str, day: date, ids: list[str]) -> str:
    """The new entry of RELEASES, formatted as ``ruff format`` leaves it."""
    when = f"date({day.year}, {day.month}, {day.day})"
    if not ids:
        return f'    Release("{version}", {when}),\n'
    inner = f'"{ids[0]}",' if len(ids) == 1 else ", ".join(f'"{i}"' for i in ids)
    one = f'    Release("{version}", {when}, ({inner})),\n'
    if len(one) <= 101:  # 100 characters and the newline
        return one
    lines = "".join(f'            "{i}",\n' for i in ids)
    head = f'    Release(\n        "{version}",\n        {when},\n'
    return f"{head}        (\n{lines}        ),\n    ),\n"


def stamp_source(text: str, version: str, day: date, ids: list[str]) -> str:
    """releases.py with the new release on top of RELEASES."""
    if MARKER not in text:
        raise ValueError("releases.py lost the line that marks where the next release goes")
    return text.replace(MARKER, MARKER + release_source(version, day, ids), 1)


def stamp_changelog(text: str, version: str, day: date) -> str:
    """CHANGELOG.md with ``## Unreleased`` renamed to the version and date, and a new empty
    ``## Unreleased`` above it for the next pull requests. A release without lines (only
    changes behind the scenes) says so."""
    heading = "## Unreleased\n"
    if heading not in text:
        raise ValueError("CHANGELOG.md has no '## Unreleased' section")
    before, after = text.split(heading, 1)
    body = after.lstrip("\n")
    empty = not body or body.startswith("## ")
    lines = "Small fixes behind the scenes.\n\n" if empty else ""
    stamped = f"{heading}\n## {version} {DASH} {day.day}-{day.month}-{day.year}\n\n{lines}"
    return before + stamped + body
