"""Calendar preview/classification in a throwaway portal, with fake provider HTTP only."""

import json
import threading
from datetime import date

import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_caldav import response
from test_caldav_setup import WRITE, FakeHTTP, connect, icloud_routes
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import calendar_mirror
from vibesynergy.sources.caldav.ics import href_for, render_event

pytestmark = pytest.mark.integration


def test_calendar_preview_and_classification_keyboard_and_axe(runtime, monkeypatch):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    target = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    key = "garmin:workout:17"
    http = FakeHTTP(icloud_routes())
    http.routes[("REPORT", target)] = response(
        [
            {
                "url": href_for(target, key),
                "etag": '"synthetic"',
                "data": render_event(
                    key,
                    date(2026, 10, 10),
                    "Synthetic obsolete workout",
                    sequence=0,
                    stamp="20261005T120000Z",
                ).decode(),
            }
        ]
    )
    monkeypatch.setattr(calendar_mirror, "garmin_coverage", lambda *_: date(2026, 12, 31))
    monkeypatch.setattr(calendar_mirror, "load_desired", lambda *_: ([], 0))
    monkeypatch.setattr("vibesynergy.portal.caldav_api.today_in", lambda _: date(2026, 10, 5))
    app = create_portal_app(runtime, caldav_http=http)
    api = TestClient(app, base_url="http://localhost")
    connect(api)
    assert (
        api.post("/api/caldav/calendars", json={"urls": [target]}, headers=WRITE).status_code == 200
    )
    assert (
        api.post("/api/caldav/garmin-target", json={"url": target}, headers=WRITE).status_code
        == 200
    )
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
                ("en", "light", 1280),
                ("nl", "dark", 390),
                ("nl", "light", 320),
                ("en", "dark", 390),
            ]:
                api.post(
                    "/api/caldav/classification",
                    json={
                        "url": target,
                        "classification": "unclassified",
                    },
                    headers=WRITE,
                )
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    locale="nl-NL" if language == "nl" else "en-GB",
                    color_scheme=theme,
                )
                context.add_init_script(axe)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.goto(base + "/#/settings/calendar")
                page.locator("[data-calendar-context]").wait_for()
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-calendar-context')"
                )
                page.keyboard.press("Home")
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Tab")
                page.locator("[data-context-status]").get_by_text(
                    "Opgeslagen." if language == "nl" else "Saved.", exact=True
                ).wait_for()
                # "Not classified" reads as an ordinary appointment, the first choice.
                assert (
                    api.get("/api/caldav").json()["data"]["selected"][0]["classification"]
                    == "coaching"
                )
                # Copying Garmin workouts lives on Settings → Sync between sources.
                page.goto(base + "/#/settings/sync")
                page.locator("[data-calendar-copy] [data-garmin-mirror-preview]").wait_for()
                assert _tab_until(
                    page, "() => document.activeElement?.hasAttribute('data-garmin-mirror-preview')"
                )
                page.keyboard.press("Enter")
                page.locator("[data-garmin-preview-body] li").wait_for()
                body = page.locator("[data-garmin-preview-body]").inner_text()
                assert "Synthetic obsolete workout" in body and "10-10-2026" in body
                page.evaluate("window.scrollTo(0, 0)")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator("[data-garmin-preview-body] summary").focus()
                page.keyboard.press("Enter")
                assert (
                    page.locator("[data-garmin-preview-body] details").get_attribute("open")
                    is not None
                )
                dialogs = []

                def cancel(dialog, dialogs=dialogs):
                    dialogs.append(dialog.message)
                    dialog.dismiss()

                page.on("dialog", cancel)
                page.locator("#garmin-mirror-enabled").focus()
                page.keyboard.press("Space")
                page.locator("[data-garmin-mirror-status]").get_by_text(
                    "Kopiëren blijft uit." if language == "nl" else "Copying stayed off.",
                    exact=True,
                ).wait_for()
                assert dialogs and ("remove 1" in dialogs[0] or "er 1" in dialogs[0])
                assert not api.get("/api/caldav").json()["data"]["mirror_enabled"]
                assert calendar_mirror._rows(runtime.engine, target) == []
                assert not errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
