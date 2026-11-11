"""The bug button: the dialog by keyboard only, axe clean, and Settings → System → Bug reports.

A throwaway portal; nothing reaches GitHub (no token, no app).
"""

import json
import threading

import pytest
import uvicorn
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy import bugs
from vibesynergy.portal.app import create_portal_app

pytestmark = pytest.mark.integration


def test_bug_dialog_by_keyboard_and_axe(runtime):
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
            for theme, width in [("dark", 390), ("light", 1280)]:
                context = browser.new_context(
                    viewport={"width": width, "height": 900}, color_scheme=theme
                )
                context.add_init_script(_axe_script())
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.goto(base + "/#/dashboard")
                page.locator("#view").wait_for()
                assert _tab_until(
                    page, "() => document.activeElement?.classList.contains('bug-report')"
                )
                page.keyboard.press("Enter")
                dialog = page.locator("dialog.bug-dialog")
                dialog.wait_for()
                assert page.evaluate("document.activeElement?.name") == "description"
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                page.keyboard.type("The chart is empty")
                page.keyboard.press("Escape")
                dialog.wait_for(state="detached")
                # Send one: it waits on this server.
                page.locator(".bug-report").click()
                page.locator("dialog.bug-dialog textarea").fill("The chart is empty")
                page.locator("dialog.bug-dialog [type=submit]").click()
                page.locator("dialog.bug-dialog h2", has_text="Thank you").wait_for()
                page.keyboard.press("Escape")
                # The settings card.
                page.goto(base + "/#/settings/system")
                details = page.locator("[data-bug-details]")
                details.locator("summary").click()
                details.locator("#bug-token").wait_for(state="attached")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert not errors, errors
                context.close()
            browser.close()
        assert len(bugs.listing(runtime.settings)) == 2
    finally:
        server.should_exit = True
