"""The workout's photos and videos (#114, phase 6): keyboard only, axe clean, NL and EN,
light and dark, desktop and phone width. Add a photo, remove it and put it back without a
mouse; reach a video's player by keyboard.

A throwaway portal on synthetic data; nothing leaves the test.
"""

from __future__ import annotations

import json
import shutil
import threading

import pytest
import uvicorn
from test_evidence import START, put
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until
from test_workout_media import picture

from vibesynergy.model import Activity
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import workout_media

pytestmark = pytest.mark.integration

COPY = {
    "nl": {"added": "Foto's toegevoegd: 1.", "back": "Teruggezet."},
    "en": {"added": "Photos added: 1.", "back": "Put back."},
}


def test_workout_photos_by_keyboard_and_axe(runtime, tmp_path):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    put(
        runtime,
        "strava",
        Activity(
            "p1",
            START,
            START.date(),
            "running",
            name="Run p1",
            distance_m=8000.0,
            moving_s=2400,
            elapsed_s=2400,
        ),
    )
    group = "strava:p1"
    # A video too (#114), when ffmpeg is there to make one: it plays in the browser's own
    # player, which the keyboard reaches like any control.
    has_video = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    if has_video:
        from test_workout_videos import make_video

        clip = make_video(tmp_path / "clip.mp4")
        workout_media.add(runtime.engine, runtime.raw.blobs, group, clip)
    app = create_portal_app(runtime)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _ready(base)
        axe = _axe_script()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            for n, (language, theme, width) in enumerate(
                [("nl", "light", 1280), ("nl", "dark", 390), ("en", "light", 390)]
            ):
                photo = tmp_path / f"photo{n}.jpg"
                photo.write_bytes(picture(10 + n))
                copy = COPY[language]
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    locale="nl-NL" if language == "nl" else "en-GB",
                    color_scheme=theme,
                )
                context.add_init_script(axe)
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.goto(f"{base}/#/activity/{group}")
                card = page.locator("[data-workout-media]")
                card.locator("[data-wm-add]").wait_for()
                assert card.locator("#wm-where").inner_text().strip()
                if has_video:
                    player = card.locator(".wm-grid video")
                    assert player.get_attribute("aria-label") and player.get_attribute("poster")
                    assert _tab_until(
                        page, "() => document.activeElement?.tagName === 'VIDEO'", steps=250
                    )
                # Add a photo with the keyboard: the button opens the file chooser.
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-wm-add')", steps=250
                )
                with page.expect_file_chooser() as chooser:
                    page.keyboard.press("Enter")
                chooser.value.set_files(str(photo))
                card.locator("[data-wm-status]").get_by_text(copy["added"], exact=True).wait_for()
                assert page.evaluate("document.activeElement?.hasAttribute('data-wm-add')")
                img = card.locator(".wm-grid img").last
                assert img.get_attribute("alt")
                page.wait_for_function(
                    "() => [...document.querySelectorAll('[data-workout-media] .wm-grid img')]"
                    ".every(i => i.complete && i.naturalWidth > 0)"
                )
                # Remove it again: it moves to the history, where it can be put back.
                live = len(workout_media.photos(runtime.engine, group)["photos"])
                button = card.locator("[data-wm-remove]").last
                button.focus()
                page.keyboard.press("Enter")
                page.wait_for_function(
                    "n => document.querySelectorAll('[data-wm-remove]').length === n", arg=live - 1
                )
                assert page.evaluate("document.activeElement?.hasAttribute('data-wm-photos')")
                summary = card.locator("[data-wm-history] summary")
                summary.focus()
                page.keyboard.press("Enter")
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-wm-restore')"
                )
                page.keyboard.press("Enter")
                card.locator("[data-wm-status]").get_by_text(copy["back"], exact=True).wait_for()
                assert len(workout_media.photos(runtime.engine, group)["photos"]) == live
                summary = card.locator("[data-wm-history] summary")
                if not summary.evaluate("s => s.parentElement.open"):
                    summary.focus()
                    page.keyboard.press("Enter")
                page.evaluate("window.scrollTo(0, 0)")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
