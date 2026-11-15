"""tools/check_help_videos.py: a change to a page a help video shows re-records that video."""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_help_videos as chv  # noqa: E402
from test_help_manuals import manual_ids  # noqa: E402

JS = ROOT / chv.JS


def test_coverage_names_exactly_the_manuals() -> None:
    assert set(chv.coverage()) == set(manual_ids())


def test_every_pattern_matches_a_file() -> None:
    files = [p.relative_to(JS).as_posix() for p in JS.rglob("*.js")]
    for video, patterns in chv.coverage().items():
        for pattern in patterns:
            assert fnmatch.filter(files, pattern), f"{video}: {pattern} matches no file"


def test_every_page_is_in_a_video_or_says_why_not() -> None:
    cover = chv.coverage()
    not_shown = json.loads(chv.COVERAGE.read_text())["_not_shown"]
    for page in sorted(p.relative_to(JS).as_posix() for p in (JS / "pages").glob("*.js")):
        shown = any(fnmatch.fnmatchcase(page, p) for patterns in cover.values() for p in patterns)
        why = f"{page}: add it to a video in coverage.json, or to _not_shown with the reason"
        assert shown or page in not_shown, why


def test_a_page_change_without_a_new_recording_is_stale() -> None:
    cover = {"coach": ["pages/coach.js", "coach_*.js"], "today": ["pages/dashboard.js"]}
    changed = [chv.JS + "coach_settings.js", chv.JS + "format.js", "CHANGELOG.md"]
    assert chv.stale(changed, cover) == {"coach": ["coach_settings.js"]}


def test_a_new_recording_settles_it() -> None:
    cover = {"coach": ["pages/coach.js"]}
    changed = [chv.JS + "pages/coach.js", chv.HELP + "coach.webm", chv.HELP + "coach.nl.vtt"]
    assert chv.stale(changed, cover) == {}


def test_captions_alone_do_not_settle_it() -> None:
    cover = {"coach": ["pages/coach.js"]}
    changed = [chv.JS + "pages/coach.js", chv.HELP + "coach.nl.vtt"]
    assert chv.stale(changed, cover) == {"coach": ["pages/coach.js"]}
