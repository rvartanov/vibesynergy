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
    Release("0.1.0-beta.0.22", date(2026, 11, 25), ("192-complete", "192-ai-complete")),
    Release("0.1.0-beta.0.21", date(2026, 11, 25), ("112-gear-line", "112-ai-gear-line")),
    Release("0.1.0-beta.0.20", date(2026, 11, 25), ("114-multisport",)),
    Release("0.1.0-beta.0.19", date(2026, 11, 25), ("188-ai-names", "188-ai-workout-name")),
    Release("0.1.0-beta.0.18", date(2026, 11, 25), ("114-archive-photos", "114-videos")),
    Release("0.1.0-beta.0.17", date(2026, 11, 25), ("114-auto-names",)),
    Release("0.1.0-beta.0.16", date(2026, 11, 24), ("182-coach-links", "182-ai-coach-links")),
    Release("0.1.0-beta.0.15", date(2026, 11, 24), ("181-workout-links",)),
    Release("0.1.0-beta.0.14", date(2026, 11, 24), ("114-photos",)),
    Release("0.1.0-beta.0.13", date(2026, 11, 24), ("170-own-words", "170-ai-own-words")),
    Release("0.1.0-beta.0.12", date(2026, 11, 24)),
    Release("0.1.0-beta.0.11", date(2026, 11, 24), ("175-publish", "175-ai-publish")),
    Release("0.1.0-beta.0.10", date(2026, 11, 23), ("114-gear", "114-ai-gear")),
    Release("0.1.0-beta.0.9", date(2026, 11, 23), ("114-insight-texts", "114-ai-insight-texts")),
    Release("0.1.0-beta.0.8", date(2026, 11, 23), ("114-text-sync", "114-ai-text-sync")),
    Release("0.1.0-beta.0.7", date(2026, 11, 23), ("114-workout-page",)),
    Release("0.1.0-beta.0.6", date(2026, 11, 23), ("114-icu-off", "114-ai-texts")),
    Release("0.1.0-beta.0.5", date(2026, 11, 22), ("117-own-domain", "117-ai-portal-url")),
    Release("0.1.0-beta.0.4", date(2026, 11, 22), ("110-plan-conflict",)),
    Release("0.1.0-beta.0.3", date(2026, 11, 22), ("125-garmin-data", "125-ai-garmin-data")),
    Release("0.1.0-beta.0.2", date(2026, 11, 22), ("91-races", "91-ai-races")),
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
    Item(
        "125-garmin-data",
        "you",
        "More Garmin data is now kept as measurements: morning readiness and what makes it up, "
        "Garmin's acute and chronic load and load focus, heat and altitude acclimation, endurance "
        "and hill score, running tolerance, lactate threshold and cycling FTP. New days fill in "
        "with every sync.",
    ),
    Item(
        "125-ai-garmin-data",
        "ai",
        "New Garmin metric ids in vibesynergy_catalog, vibesynergy_series and "
        "vibesynergy_day: recovery.readiness.morning and its factors "
        "(recovery.readiness.sleep, .hrv, .acute_load, …), recovery.recovery_time, "
        "training.load.acute/chronic/ratio, training.load_balance.*, "
        "training.running_tolerance, fitness.acclimation.*, fitness.endurance_score, "
        "fitness.hill_score.*, fitness.lactate_threshold.* and fitness.cycling_ftp. "
        "vibesynergy_activity has workout_steps and rounds for Garmin structured workouts "
        "and HIIT; each lap names its step in extra.wktStepIndex.",
    ),
    Item(
        "110-plan-conflict",
        "you",
        "When another app or device keeps overwriting your plan in intervals.icu, Garmin or "
        "your calendar, you get an alert that says where, which sessions and since when. After "
        "a few times VibeSynergy stops putting it back until you confirm under Waiting for "
        "you → Notices.",
    ),
    Item(
        "117-own-domain",
        "you",
        "Links in mails, notices and push now always open VibeSynergy on its own domain, "
        "also when you asked for them on another address. Settings → System shows that "
        "address, with the other addresses it can also be reached at.",
    ),
    Item(
        "117-ai-portal-url",
        "ai",
        "Every MCP result now carries meta.portal_url: the portal's own address. A link "
        "such as #/coach/report/7 opens at that address followed by the link.",
    ),
    Item(
        "114-icu-off",
        "you",
        "VibeSynergy no longer puts its block in intervals.icu: intervals.icu passed it on to "
        "Strava, and your own text there got lost. Strava and Garmin work as before.",
    ),
    Item(
        "114-ai-texts",
        "ai",
        "Every workout now has its own name, public text and private text in VibeSynergy, with "
        "a history of versions: vibesynergy_activity_texts reads them, "
        "vibesynergy_activity_set_texts writes them and vibesynergy_activity_restore_text "
        "restores an old version. The private text is context only, never for a public text.",
    ),
    Item(
        "114-workout-page",
        "you",
        "On the workout page you now edit the name, your public text and your private "
        "text, see every earlier version and put one back. The Publish tick puts your AI "
        "coach's insight and the figures on Strava; in Settings you choose whether new "
        "workouts start public or only for you.",
    ),
    Item(
        "114-text-sync",
        "you",
        "You can now keep a published workout's name and public text the same in "
        "VibeSynergy, on Strava and on Garmin: switch it on in Settings → Sync between "
        "sources. A change in one place reaches the others within minutes, and the "
        "workout's history shows what came from where.",
    ),
    Item(
        "114-ai-text-sync",
        "ai",
        "With the text sync on, a published workout's name and public text follow edits "
        "made on Strava and Garmin: texts can change with source strava or garmin, and a "
        "version that lost to a later change has kind overwritten. The private text never "
        "leaves VibeSynergy.",
    ),
    Item(
        "114-insight-texts",
        "you",
        "Your AI coach's insight now follows your workout texts. Publishing a workout "
        "in VibeSynergy has the insight written when there is none yet. Change the name "
        "or a text afterwards (here, or on Strava or Garmin) and your AI coach writes "
        "the insight again a few minutes later. Not right? Correct it on the workout "
        "page: only you see your correction, and the insight is rewritten with it in "
        "mind. Earlier insights stay in the history.",
    ),
    Item(
        "114-ai-insight-texts",
        "ai",
        "The insight run now also starts from VibeSynergy's own texts: after the "
        "Publish tick (the first insight), after a change of the name or a text (a "
        "rewrite of that one insight, a few minutes after the last edit, at most a few "
        "per workout per day) and after the athlete's correction of the insight. Its "
        "context has a new insight_rewrite block: the current insight, the texts "
        "changed after it and the athlete's corrections, which are private. A "
        "public_insight that quotes the private text or a correction is refused. "
        "vibesynergy_activity_texts lists the corrections with field correction.",
    ),
    Item(
        "114-gear",
        "you",
        "Link gear to a workout with one tap: its page suggests the gear you most "
        "likely used and says why. Add swim gear of your own, such as a pull buoy, on "
        "the Gear page, and match your gear with Garmin's once. The switch that keeps "
        "a published workout's name and texts the same on Strava and Garmin now keeps "
        "its gear the same too.",
    ),
    Item(
        "114-ai-gear",
        "ai",
        "vibesynergy_gear now also lists gear the athlete added in VibeSynergy (source "
        "vs, for example kind swim) without a km total. A workout can have one main "
        "item (bike or shoes) plus extra items; with the text sync on, Strava gets the "
        "main item and Garmin the items matched to its gear.",
    ),
    Item(
        "175-publish",
        "you",
        "Settings → Publishing and sync: choose where a published workout goes, Strava, "
        "Garmin or both. Each place keeps its own text with VibeSynergy's section below "
        "it; switch on keeping name, texts and gear the same to make them match. Your "
        "earlier Garmin choice carries over.",
    ),
    Item(
        "175-ai-publish",
        "ai",
        "Publishing a workout now goes to every place the athlete chose ([publication] "
        "targets: strava and/or garmin): vibesynergy_activity_publish and _unpublish "
        "return the other place's outcome in others. The old Strava to Garmin copy is "
        "gone; only the text sync writes names and public texts to Garmin.",
    ),
    Item(
        "170-own-words",
        "you",
        "Your AI coach now keeps what you wrote in the last 9 days in mind in every piece of "
        "advice: your check-in notes, complaints, diary notes and the name, texts and notes of "
        "each workout. Yesterday's \"shoulder felt stiff\" is in today's advice.",
    ),
    Item(
        "170-ai-own-words",
        "ai",
        "Every advice task (morning, evening, week, check-in, injury, reply) and the insight get "
        "context block recent_words: the athlete's own words of the last [coach_runner] "
        "recent_words_days days (default 9), newest first. Today and yesterday are verbatim; an "
        "older day marked samengevat is shortened, with complaints verbatim, and "
        "vibesynergy_notes, vibesynergy_injuries and vibesynergy_activity_texts give the full "
        "text. Today's check-in note is only in this block. The insight gets only names and "
        "public texts of the other workouts.",
    ),
    Item(
        "114-photos",
        "you",
        "Add photos to a workout on its page. The photos on its Strava and Garmin copies "
        "come in by themselves, each photo once, and show on share links too. Strava and "
        "Garmin take no photos from other apps, so the ones you add stay in VibeSynergy.",
    ),
    Item(
        "181-workout-links",
        "you",
        "A workout named in an advice, a plan change or Coming up now opens to its steps, "
        "and once done it takes you to the activity. On the plan page a done workout links "
        "to its activity too.",
    ),
    Item(
        "182-coach-links",
        "you",
        'The card "AI coach after a workout" under Coach → Settings is gone: nothing used it '
        "any more. Your AI coach in VibeSynergy still writes its insight as soon as you make a "
        "workout public.",
    ),
    Item(
        "182-ai-coach-links",
        "ai",
        "VibeSynergy no longer sends events to AI apps over webhooks (the outgoing coach links "
        "are gone). Read activity.ready, activity.published and coach.run_missed with "
        "vibesynergy_events; the vibesynergy.test event no longer exists.",
    ),
    Item(
        "114-auto-names",
        "you",
        "New workouts get a name by themselves: the title of their plan session, or else "
        'sport, part of the day and place, such as "Morning run Utrecht". A name you change '
        "yourself stays. Switch it off under Settings → Publishing and sync.",
    ),
    Item(
        "114-archive-photos",
        "you",
        "Importing your Strava archive now also brings the photos of your workouts, each "
        "photo once.",
    ),
    Item(
        "114-videos",
        "you",
        "Workouts can have videos now too. Add an MP4 or MOV on the workout page; the videos "
        "on Strava and in your Strava archive come in by themselves. They play on the workout "
        "page and on share links, without the place they were filmed.",
    ),
    Item(
        "188-ai-names",
        "you",
        "The AI coach now names new workouts when it writes the insight, after what you "
        "actually did, such as a Zwift ride. It no longer copies the plan title. A name "
        "you change yourself stays.",
    ),
    Item(
        "188-ai-workout-name",
        "ai",
        "vibesynergy_coach_write takes workout_name with the public insight: a short "
        "name for what was actually done, stored as the workout's name unless it is "
        "locked or the setting is off (harness rules v9).",
    ),
    Item(
        "114-multisport",
        "you",
        "A brick or triathlon you publish now gets its text on every part on Strava, with "
        "a short part line and the workout's name with the part. Garmin gets it once. A "
        "name you change on one part stays on that part.",
    ),
    Item(
        "112-gear-line",
        "you",
        "A planned workout that needs gear now says so at the top of its notes, like "
        '"Materiaal: pullbuoy, paddles": on the start page, in This week, in Garmin and '
        "in intervals.icu.",
    ),
    Item(
        "112-ai-gear-line",
        "ai",
        "Harness rules v10: when the AI coach writes or changes a session that needs "
        'gear, it puts one line "Materiaal: …" at the top of the description; '
        "VibeSynergy adds it from the steps when it is missing.",
    ),
    Item(
        "192-complete",
        "you",
        "Every planned workout is now complete on its own: the AI coach writes every "
        'drill and step out by name, never "same drills as last week", so you have '
        "everything on the watch and the gear line sees what you need.",
    ),
    Item(
        "192-ai-complete",
        "ai",
        "Harness rules v11: every session the AI coach writes is complete on its own, "
        "with each drill and step named with its cue and no reference to another "
        "session for its content; a reference with nameless steps is refused "
        "(incomplete_reference), a reference with named steps is a warning.",
    ),
    Item(
        "coach-page-speed",
        "you",
        "The Coach page opens quickly again: it no longer waits for the whole message "
        "list, and that list is about ten times faster.",
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
