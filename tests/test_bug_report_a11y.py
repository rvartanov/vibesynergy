"""The bug button: on every page, by keyboard, and it opens GitHub's bug report form with the
page's context in its fields. A throwaway portal; GitHub itself is not contacted."""

import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy.portal.app import create_portal_app

pytestmark = pytest.mark.integration


def test_bug_button_opens_a_new_github_issue(runtime):
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
                asked = []

                def github(route, request, asked=asked):
                    asked.append(request.url)
                    route.fulfill(status=200, body="", content_type="text/html")

                context.route("https://github.com/**", github)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.goto(base + "/#/dashboard?day=2026-11-11")
                page.locator("#view").wait_for()
                button = page.locator(".bug-report")
                assert button.is_visible()
                assert "GitHub" in (button.get_attribute("aria-label") or "")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert _tab_until(
                    page, "() => document.activeElement?.classList.contains('bug-report')"
                )
                page.wait_for_timeout(300)  # the version and the address are fetched first
                with context.expect_page() as opened:
                    page.keyboard.press("Enter")
                opened.value.wait_for_load_state()
                url = urlsplit(asked[-1])
                assert (url.netloc, url.path) == ("github.com", "/rvartanov/vibesynergy/issues/new")
                query = parse_qs(url.query)
                assert query["template"] == ["bug_report.yml"]
                assert query["title"][0].startswith("Bug:")
                assert query["page"] == ["#/dashboard"]  # the route only, not "?day=…"
                assert query["version"][0].startswith("0.") and query["system"][0]
                opened.value.close()
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
