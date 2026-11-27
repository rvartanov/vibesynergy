"""Owner Diary: the quick log is on the first screen, and empty saves stay quiet."""

from __future__ import annotations

import pytest
from browser_helpers import portal_server, require_browser

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import release_news

pytestmark = pytest.mark.integration


@pytest.fixture
def portal(runtime):
    chrome = require_browser(axe=False)
    # The everyday start page: the "What changed" card of a release (#140) shows once,
    # above everything, and is already seen here.
    release_news.news(runtime.engine)
    with portal_server(create_portal_app(runtime)) as base:
        yield base, chrome


@pytest.fixture
def browser(portal):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(executable_path=portal[1], headless=True)
        yield launched
        launched.close()


def _page(browser, **viewport):
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return page, page_errors


def test_portal_quicklog(portal, browser):
    base = portal[0]
    page, errors = _page(browser, width=390, height=844)
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


def test_empty_checkin_and_stale_flash(portal, browser):
    base = portal[0]
    page, errors = _page(browser, width=1280, height=900)
    posts: list[str] = []
    page.on(
        "request",
        lambda request: (
            posts.append(request.url)
            if request.method == "POST" and "diary/checkin" in request.url
            else None
        ),
    )
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
