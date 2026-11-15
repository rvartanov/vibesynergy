"""Help → video manuals: every manual the page lists has its video, poster and captions."""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from vibesynergy.portal.app import create_portal_app

STATIC = Path(__file__).parents[1] / "src" / "vibesynergy" / "portal" / "static"
HELP = STATIC / "help"
FILES = (".webm", ".jpg", ".nl.vtt", ".en.vtt")
# A manual is a short clip: keep the package small.
MAX_VIDEO_BYTES = 1_000_000


def manual_ids() -> list[str]:
    source = (STATIC / "js" / "pages" / "help.js").read_text()
    return re.findall(r'^\s*\["([a-z-]+)", N\(', source, flags=re.M)


def test_the_help_page_lists_manuals() -> None:
    ids = manual_ids()
    assert len(ids) >= 10
    assert len(ids) == len(set(ids)), "a manual is listed twice"


def test_every_manual_has_its_files() -> None:
    for manual in manual_ids():
        for suffix in FILES:
            assert (HELP / f"{manual}{suffix}").is_file(), f"missing help/{manual}{suffix}"
        assert (HELP / f"{manual}.webm").stat().st_size <= MAX_VIDEO_BYTES, manual


def test_no_stray_files_in_help() -> None:
    expected = {f"{manual}{suffix}" for manual in manual_ids() for suffix in FILES}
    assert {p.name for p in HELP.iterdir()} == expected


def test_captions_are_webvtt_with_cues() -> None:
    cue = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}$", re.M)
    for manual in manual_ids():
        for lang in ("nl", "en"):
            text = (HELP / f"{manual}.{lang}.vtt").read_text()
            assert text.startswith("WEBVTT\n"), f"{manual}.{lang}.vtt"
            assert len(cue.findall(text)) >= 3, f"{manual}.{lang}.vtt has too few cues"
        nl = (HELP / f"{manual}.nl.vtt").read_text()
        en = (HELP / f"{manual}.en.vtt").read_text()
        assert cue.findall(nl) == cue.findall(en), f"{manual}: nl and en captions differ in timing"


def test_manual_files_are_served_with_their_types(runtime) -> None:
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    build_id = client.get("/version").json()["build_id"]
    base = f"/static/v/{build_id}/help/login"
    video = client.get(f"{base}.webm")
    assert video.status_code == 200
    assert video.headers["content-type"] == "video/webm"
    assert client.get(f"{base}.nl.vtt").headers["content-type"].startswith("text/vtt")
    assert client.get(f"{base}.jpg").headers["content-type"] == "image/jpeg"
    page = client.get("/")
    assert 'class="foot-link help-link" href="#/help"' in page.text
