"""The workout's gear and the gear mapping (#114, phase 5): keyboard only, axe clean, NL and
EN, light and dark, desktop and phone width. Link the suggested gear with one key press,
pick other gear in the picker, use the suggested Garmin matches on the Gear page and add
swim gear of your own, all without a mouse.

A throwaway portal on synthetic data; nothing leaves the test.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
import uvicorn
from test_evidence import START, put
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until
from test_workout_gear import _snapshots

from vibesynergy.model import Activity
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import gear_map, workout_gear

pytestmark = pytest.mark.integration

COPY = {
    "nl": {"saved": "Opgeslagen.", "linked": "Gekoppeld: Speedgoat 5.", "none": "Geen materiaal"},
    "en": {"saved": "Saved.", "linked": "Linked: Speedgoat 5.", "none": "No gear"},
}


def _act(rt, sid, start, gear_id=None, source="strava"):
    extra = {"gear_id": gear_id} if gear_id else {}
    put(
        rt,
        source,
        Activity(
            sid,
            start,
            start.date(),
            "running",
            name=f"Run {sid}",
            distance_m=8000.0,
            moving_s=2400,
            elapsed_s=2400,
            extra=extra,
        ),
    )
    return f"{source}:{sid}"


def test_workout_gear_and_mapping_by_keyboard_and_axe(runtime):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    _snapshots(runtime)
    _act(runtime, "r1", START - timedelta(days=2), "g2")
    group = _act(runtime, "r2", START, source="garmin")
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
            for language, theme, width in [
                ("nl", "light", 1280),
                ("nl", "dark", 390),
                ("en", "light", 390),
            ]:
                # Start each pass with nothing linked and nothing matched.
                workout_gear.set_gear(
                    runtime.engine, runtime.raw, group, main=None, extras=[], today=START.date()
                )
                with runtime.engine.begin() as conn:
                    from vibesynergy.store.schema import gear_map as gear_map_table

                    conn.execute(gear_map_table.delete())
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
                page.locator("[data-workout-gear] [data-wg-link]").wait_for()
                # The reason is said in words and tied to the button.
                assert page.locator("#wg-reason").inner_text().strip()
                assert (
                    page.locator("[data-wg-link]").get_attribute("aria-describedby") == "wg-reason"
                )
                # One tap: the suggested gear, by keyboard.
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-wg-link')", steps=200
                )
                page.keyboard.press("Enter")
                page.locator("[data-wg-status]").get_by_text(copy["saved"], exact=True).wait_for()
                assert page.evaluate("document.activeElement?.hasAttribute('data-wg-linked')")
                assert page.locator("[data-wg-linked]").inner_text().strip() == copy["linked"]
                assert workout_gear.current(runtime.engine, group)["main"] == "strava:g2"
                assert page.locator("[data-wg-link]").count() == 0  # nothing left to suggest
                # The picker: open it, choose "no gear", save, all with the keyboard.
                assert _tab_until(
                    page, "() => document.activeElement?.matches('[data-wg-picker] summary')"
                )
                page.keyboard.press("Enter")
                assert _tab_until(page, "() => document.activeElement?.id === 'wg-main'")
                page.keyboard.press("End")
                assert page.locator("#wg-main").evaluate("s => s.value") == "none"
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-wg-save')"
                )
                page.keyboard.press("Enter")
                page.locator("[data-wg-status]").get_by_text(copy["saved"], exact=True).wait_for()
                assert page.evaluate("document.activeElement?.hasAttribute('data-wg-save')")
                now = workout_gear.current(runtime.engine, group)
                assert now["main"] is None and now["how"] == "own"
                for summary in page.locator("[data-workout-gear] summary").all():
                    if not summary.evaluate("s => s.parentElement.open"):
                        summary.focus()
                        page.keyboard.press("Enter")
                page.evaluate("window.scrollTo(0, 0)")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                # The Gear page: use the suggested Garmin matches, add swim gear of your own.
                page.goto(f"{base}/#/gear")
                page.locator("[data-gear-map-accept]").wait_for()
                assert _tab_until(
                    page,
                    "() => document.activeElement?.hasAttribute('data-gear-map-accept')",
                    steps=300,
                )
                page.keyboard.press("Enter")
                page.locator("[data-gear-map-status]").get_by_text(
                    copy["saved"], exact=True
                ).wait_for()
                assert gear_map.garmin_of(runtime.engine)["strava:g1"] == "u-peg"
                assert page.evaluate("document.activeElement?.hasAttribute('data-gear-map-ref')")
                summary = page.locator(".gear-own summary")
                summary.focus()
                page.keyboard.press("Enter")
                assert _tab_until(page, "() => document.activeElement?.id === 'gear-own-name'")
                page.keyboard.type(f"Pullbuoy {language} {theme}")
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-gear-own-add')"
                )
                page.keyboard.press("Enter")
                page.locator("[data-gear-own-status]").wait_for()
                page.wait_for_function(
                    "() => document.querySelector('[data-gear-own-status]')?.textContent"
                )
                names = [i["name"] for i in gear_map.own_items(runtime.engine)]
                assert f"Pullbuoy {language} {theme}" in names
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
