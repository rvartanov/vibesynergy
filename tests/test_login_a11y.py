"""The login page and Settings → Account by keyboard only, axe clean, phone and desktop.

A throwaway portal with the login gate on; nothing is mailed.
"""

import json
import re
import threading

import pytest
import uvicorn
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import auth

pytestmark = [pytest.mark.integration, pytest.mark.auth]

EMAIL = "athlete@example.com"
GOOD = "lange-zin-met-fietsen-2027"


def test_login_and_account_by_keyboard_and_axe(runtime):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    app = create_portal_app(runtime)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        _ready(base + "/login")
        axe = _axe_script()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            for language, theme, width in [("nl", "dark", 390), ("en", "light", 1280)]:
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    locale="nl-NL" if language == "nl" else "en-GB",
                    color_scheme=theme,
                )
                context.add_init_script(axe)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                # Any page sends you to the login first.
                page.goto(base + "/#/settings/account")
                page.wait_for_url(re.compile(r"/login\?next="))
                page.locator("#email").wait_for()
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                # Keyboard only: email, password, Enter.
                page.locator("#email").focus()
                page.keyboard.type(EMAIL)
                page.keyboard.press("Tab")
                page.keyboard.type(GOOD)
                page.keyboard.press("Enter")
                page.wait_for_url(base + "/")
                # The account section: password, second step, devices, log out.
                page.goto(base + "/#/settings/account")
                page.locator("#account-title").wait_for()
                assert EMAIL in page.locator("[data-account-host]").inner_text()
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert _tab_until(
                    page, "() => document.activeElement?.dataset?.logout !== undefined", steps=200
                )
                page.keyboard.press("Enter")
                page.wait_for_url(re.compile(r"/login$"))
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
