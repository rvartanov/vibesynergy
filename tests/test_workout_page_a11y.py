"""The workout page's texts (#114): keyboard only, axe clean, NL and EN, light and dark,
desktop and phone width. Correct the AI-coach insight, edit the public text, look at its
history, restore a version and tick Publish, all without a mouse.

A throwaway portal on synthetic data; the Strava provider is a fake, so nothing leaves the
test.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
import uvicorn
from test_evidence import put
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until
from test_publication import garmin_activity
from test_publication_targets import FakeStrava

from vibesynergy.core import config_edit
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_workouts, publication, publication_plugins, workout_texts

pytestmark = pytest.mark.integration

SECRET = "Linkerkuit iets stijf."
COPY = {
    "nl": {"saved": "Opgeslagen.", "restored": "Teruggezet.", "published": "Gepubliceerd."},
    "en": {"saved": "Saved.", "restored": "Restored.", "published": "Published."},
}


def test_workout_texts_by_keyboard_and_axe(runtime, monkeypatch):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(publication_plugins, "_weather", lambda *_, **__: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_, **__: None)
    fake = FakeStrava()
    monkeypatch.setattr(publication, "provider", lambda _rt, _dest: fake)
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"targets": ["strava"]}}
    )
    a = garmin_activity()
    put(runtime, "garmin", a)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title", fit_id=None))
    group = "garmin:1"
    workout_texts.set_texts(runtime.engine, group, public="Eerste versie", private=SECRET)
    coach_workouts.coach_write(
        runtime.engine,
        summary="Prive.",
        entry_type="activity_assessment",
        for_date=a.start_utc.date(),
        activity_id=group,
        public_insight="Duurloop met de groep, netjes in Z2.",
    )

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
            for language, theme, width in [("nl", "light", 1280), ("nl", "dark", 390),
                                           ("en", "light", 390)]:  # fmt: skip
                if fake.writes:  # start each pass unpublished
                    from vibesynergy.query import workout_page

                    workout_page.set_published(runtime, group, False)
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
                page.locator("[data-workout-texts] #wt-public").wait_for()
                # The two parts are named, and each says who sees it.
                assert page.locator("#wt-public-title").count() == 1
                assert page.locator("#wt-private-title").count() == 1
                assert SECRET in page.locator("#wt-private").input_value()
                # Correct the insight (#114): the label, the help and the status, by keyboard.
                fix = f"Ik liep alleen ({language}, {theme})"
                assert _tab_until(
                    page, "() => document.activeElement?.id === 'wt-correction'", steps=200
                )
                page.keyboard.type(fix)
                page.keyboard.press("Tab")
                assert page.evaluate("document.activeElement?.hasAttribute('data-wt-correct')")
                page.keyboard.press("Enter")
                page.locator("[data-wt-correct-status]").get_by_text(
                    copy["saved"], exact=True
                ).wait_for()
                assert page.evaluate("document.activeElement?.hasAttribute('data-wt-correct')")
                assert workout_texts.corrections(runtime.engine, group)[0]["text"] == fix
                assert page.locator("[data-wt-rewrite]").inner_text().strip()
                text = f"Mooie ronde ({language}, {theme})"
                assert _tab_until(
                    page, "() => document.activeElement?.id === 'wt-public'", steps=200
                )
                page.keyboard.press("Control+A")
                page.keyboard.type(text)
                page.keyboard.press("Tab")  # leaving the field saves it; focus stays on Save
                status = page.locator("[data-wt-field=public] [data-wt-status]")
                status.get_by_text(copy["saved"], exact=True).wait_for()
                assert page.evaluate("document.activeElement?.hasAttribute('data-wt-save')")
                assert workout_texts.texts(runtime.engine, group)["public"]["text"] == text
                # History: open it and restore the version before this one.
                assert _tab_until(
                    page,
                    "() => document.activeElement?.matches('[data-wt-field=public] summary')",
                )
                page.keyboard.press("Enter")
                assert _tab_until(
                    page,
                    "() => document.activeElement?.matches("
                    "'[data-wt-field=public] [data-wt-restore]')",
                )
                label = page.evaluate("document.activeElement.getAttribute('aria-label')")
                visible = page.evaluate("document.activeElement.textContent")
                assert visible in label  # the spoken name holds the visible word
                page.keyboard.press("Enter")
                status.get_by_text(copy["restored"], exact=True).wait_for()
                assert page.evaluate("document.activeElement?.id") == "wt-public"
                assert workout_texts.texts(runtime.engine, group)["public"]["text"] != text
                # The Publish tick, with Space.
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-wt-publish')"
                )
                page.keyboard.press("Space")
                page.locator("[data-wt-publish-status]").get_by_text(
                    copy["published"], exact=True
                ).wait_for()
                assert page.locator("[data-wt-publish]").is_checked()
                assert fake.writes and SECRET not in fake.writes[-1]
                for summary in page.locator("[data-workout-texts] summary").all():
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
