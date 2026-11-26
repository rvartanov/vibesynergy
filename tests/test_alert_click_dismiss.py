"""A notice at the top goes away once its link is clicked, as with its close button.

The notice is made up."""

from __future__ import annotations

import json
import threading

import pytest

from vibesynergy.core.config import load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration

ALERTS = {
    "data": {
        "open": [
            {
                "key": "made-up:notice",
                "kind": "notice",
                "level": "warn",
                "text": "A made-up notice",
                "link": "#/settings",
            }
        ],
        "recent": [],
    }
}


def test_clicking_a_notice_link_hides_the_notice(tmp_path, monkeypatch) -> None:
    from test_portal_a11y import _port, _ready, _require_tools

    chrome = _require_tools()
    import uvicorn
    from playwright.sync_api import sync_playwright

    port = _port()
    config = tmp_path / "config.toml"
    config.write_text(f'[user]\ntimezone = "Europe/Amsterdam"\n[portal]\nport = {port}\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    server = uvicorn.Server(
        uvicorn.Config(create_portal_app(rt), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _ready(base)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            page = browser.new_page()
            page.route(
                "**/alerts",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(ALERTS)
                ),
            )
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            notice = page.locator(".header-warn-notices a", has_text="A made-up notice")
            notice.wait_for(timeout=20000)
            notice.click()
            page.wait_for_function("location.hash.startsWith('#/settings')", timeout=10000)
            page.locator(".header-warn-notices").wait_for(state="detached", timeout=10000)
            stored = page.evaluate("localStorage.getItem('vibesynergy-dismissed-alerts')")
            assert "made-up:notice" in stored
            # Still gone after a reload: the dismissal is remembered on this device.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            assert page.locator(".header-warn-notices").count() == 0
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        rt.close()
