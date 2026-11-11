"""Settings → Sync: AI coach links by keyboard only, axe clean, NL and EN.

A throwaway portal; the routine's HTTP exchange is a fake, so nothing leaves the test.
"""

import json
import threading

import pytest
import uvicorn
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import agent_events

pytestmark = pytest.mark.integration

TOKEN = "sk-ant-oat01-SYNTHETIC-a11y-token"
SESSION = "https://claude.ai/code/session_synthetic"


def test_agent_links_keyboard_and_axe(runtime, monkeypatch):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    calls = []

    def exchange(url, headers, body):
        calls.append(url)
        return agent_events.Reply(200, {}, json.dumps({"claude_code_session_url": SESSION}))

    monkeypatch.setattr(agent_events, "_requests_exchange", exchange)
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
            for language, theme, width in [("en", "light", 1280), ("nl", "dark", 390)]:
                for sub in agent_events.subscriptions(runtime):
                    agent_events.delete_subscription(runtime, sub["id"])
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    locale="nl-NL" if language == "nl" else "en-GB",
                    color_scheme=theme,
                )
                context.add_init_script(axe)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.goto(base + "/#/coach/settings")
                page.locator("[data-agent-add] summary").wait_for()
                assert _tab_until(
                    page,
                    "() => document.activeElement?.matches('[data-agent-add] summary')",
                    steps=120,
                )
                page.keyboard.press("Enter")  # the add form starts folded
                page.keyboard.press("Tab")
                assert page.evaluate("document.activeElement?.name") == "name"
                page.keyboard.type("Coach na training")
                page.keyboard.press("Tab")  # kind: the Claude Code routine is preselected
                page.keyboard.press("Tab")
                page.keyboard.type("trig_01SYNTHETIC")
                page.keyboard.press("Tab")
                page.keyboard.type(TOKEN)
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-agent-create')"
                )
                page.keyboard.press("Enter")
                added = (
                    "Koppeling toegevoegd. Zet hem aan wanneer je zover bent."
                    if language == "nl"
                    else "Link added. Switch it on when you are ready."
                )
                page.locator("[data-agent-status]").get_by_text(added, exact=True).wait_for()
                assert page.evaluate("document.activeElement?.name") == "enabled"
                page.keyboard.press("Space")
                page.locator("[data-agent-status]").get_by_text(
                    "Opgeslagen." if language == "nl" else "Saved.", exact=True
                ).wait_for()
                (saved,) = agent_events.subscriptions(runtime)
                assert saved["enabled"] and saved["has_secret"]
                assert saved["url"].endswith("/routines/trig_01SYNTHETIC/fire")
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-agent-test')"
                )
                page.keyboard.press("Enter")
                page.locator(f"[data-agent-status] a[href='{SESSION}']").wait_for()
                for summary in page.locator("[data-agent-link] summary").all():
                    summary.focus()
                    page.keyboard.press("Enter")
                page.evaluate("window.scrollTo(0, 0)")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert TOKEN not in page.content()
                assert not errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert len(calls) == 2  # one test per language
