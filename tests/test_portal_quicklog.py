"""Owner Diary: the quick log is on the first screen, and empty saves stay quiet."""

from __future__ import annotations

import threading

import pytest
import uvicorn

from vibesynergy.portal.app import create_portal_app


def _server(runtime):
    from test_portal_a11y import _chrome, _port, _ready

    chrome = _chrome()
    if chrome is None:
        pytest.skip("google-chrome is not installed")
    pytest.importorskip("playwright.sync_api")
    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(create_portal_app(runtime), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    _ready(base)
    return server, base, chrome


def _browser(chrome, **viewport):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(executable_path=chrome, headless=True)
    context = browser.new_context(viewport=viewport or {"width": 390, "height": 844})
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return playwright, browser, page, page_errors


def test_portal_quicklog(runtime):
    server, base, chrome = _server(runtime)
    playwright, browser, page, errors = _browser(chrome, width=390, height=844)
    try:
        # #/today lands on the start page; the check-in card holds the quick log.
        page.goto(base + "/#/today")
        quick = page.locator("[data-dash-checkin]")
        quick.wait_for()
        assert quick.count() == 1
        assert page.locator(".diary-checkin").count() == 0
        box = quick.bounding_box()
        assert box is not None and box["y"] < 900
        for field in ("sleep_quality", "soreness", "mood", "stress", "motivation"):
            dot = page.locator(f'.ql-dot[data-field="{field}"][data-value="3"]')
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and "diary/checkin" in response.url
                    and response.ok
                )
            ):
                dot.click()
            if field != "motivation":
                page.locator(
                    f'.ql-dot[data-field="{field}"][data-value="3"][aria-pressed="true"]'
                ).wait_for()
        # All five answered: the start page folds the check-in to one line.
        page.locator(".checkin-done").wait_for()
        saved = page.request.get(base + "/api/diary?period=last_7d")
        assert saved.ok
        checkins = saved.json()["data"]["checkins"]
        row = checkins[-1]
        assert row["sleep_quality"] == row["soreness"] == row["mood"] == 3
        assert row["stress"] == row["motivation"] == 3
        assert not errors
    finally:
        browser.close()
        playwright.stop()
        server.should_exit = True


def test_empty_checkin_and_stale_flash(runtime):
    server, base, chrome = _server(runtime)
    playwright, browser, page, errors = _browser(chrome, width=1280, height=900)
    posts: list[str] = []
    page.on(
        "request",
        lambda request: (
            posts.append(request.url)
            if request.method == "POST" and "diary/checkin" in request.url
            else None
        ),
    )
    try:
        # #/diary/entry opens the diary section of the start page. The check-in has
        # one place, the quick log at the top; the diary keeps the full note and pain forms.
        page.goto(base + "/#/diary/entry")
        page.locator("#diary details[open] .diary-note button[type=submit]").wait_for()
        assert page.locator(".diary-checkin").count() == 0
        assert page.locator("[data-dash-checkin] .ql-dot").count() > 0
        page.locator(".diary-note button[type=submit]").click()
        page.locator(".diary-note textarea:invalid").wait_for()
        assert not posts
        assert not errors
    finally:
        browser.close()
        playwright.stop()
        server.should_exit = True
