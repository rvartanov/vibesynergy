"""A day in the future opens (#8): what is planned, no measurements, and the stepper and the
date picker go on past today. Keyboard and axe. A throwaway portal."""

import json
import threading
import urllib.request
from datetime import date, timedelta

import pytest
import uvicorn
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools

from vibesynergy.portal.app import create_portal_app

pytestmark = pytest.mark.integration


def test_a_future_day_opens_with_what_is_planned(runtime):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    app = create_portal_app(runtime)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        _ready(base + "/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 900})
            context.add_init_script(_axe_script())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            with urllib.request.urlopen(f"{base}/api/meta") as reply:
                today = json.loads(reply.read())["data"]["today"]
            coming = (date.fromisoformat(today) + timedelta(days=5)).isoformat()
            page.goto(f"{base}/#/dashboard/{coming}")
            page.locator("#dash-plan-title").wait_for()
            assert page.locator("#dash-plan-title").inner_text() == "Planned"
            # A coming day has nothing measured: no key numbers, sleep or timeline.
            assert page.locator("[data-day-numbers]").count() == 0
            assert page.locator("[data-timeline]").count() == 0
            picker = page.locator("[data-day-picker]")
            assert picker.input_value() == coming and picker.get_attribute("max") is None
            # The next day is a link here too, and leads further into the future.
            page.locator(".stepper a[aria-label='Next day']").focus()
            page.keyboard.press("Enter")
            later = (date.fromisoformat(coming) + timedelta(days=1)).isoformat()
            for _ in range(50):
                if page.url.endswith(later):
                    break
                page.wait_for_timeout(100)
            assert page.url.endswith(f"#/dashboard/{later}")
            page.locator("#dash-plan-title").wait_for()
            violations = _axe(page)
            assert not violations, json.dumps(violations, indent=2)
            # Choosing a date after today in the picker opens that day, not today.
            picker = page.locator("[data-day-picker]")
            far = (date.fromisoformat(today) + timedelta(days=20)).isoformat()
            picker.fill(far)
            picker.dispatch_event("change")
            for _ in range(50):
                if page.url.endswith(far):
                    break
                page.wait_for_timeout(100)
            assert page.url.endswith(f"#/dashboard/{far}")
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
