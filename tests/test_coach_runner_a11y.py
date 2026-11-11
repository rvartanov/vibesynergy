"""Coach via API settings and report replies by keyboard only, axe clean, NL and EN.

A throwaway portal; nothing calls the model or sends mail.
"""

import json
import threading
from datetime import date

import pytest
import uvicorn
from sqlalchemy import select
from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools, _tab_until

from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_workouts
from vibesynergy.store.schema import coach_reply

pytestmark = pytest.mark.integration

KEY = "sk-ant-api03-SYNTHETIC-a11y-key-never-shown"


def test_coach_runner_keyboard_and_axe(runtime):
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    entry = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Alex, houd de duurloop vandaag rustig.",
        details="HRV binnen band.",
        idempotency_key="morning-a11y",
        for_date=date(2026, 11, 1),
        author="client:coach-runner",
    )
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
                runner_settings.clear_api_key(runtime)
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    locale="nl-NL" if language == "nl" else "en-GB",
                    color_scheme=theme,
                )
                context.add_init_script(axe)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                saved = "Opgeslagen." if language == "nl" else "Saved."

                # Coach → Settings: paste the key, then switch it on, by keyboard only.
                page.goto(base + "/#/coach/settings")
                page.locator("#coach-runner-key-anthropic").wait_for()
                assert page.locator("[data-coach-runner] input[name=enabled]").is_disabled()
                assert _tab_until(
                    page,
                    "() => document.activeElement?.id === 'coach-runner-key-anthropic'",
                    steps=150,
                )
                page.keyboard.type(KEY)
                page.keyboard.press("Tab")
                assert page.evaluate(
                    "() => document.activeElement?.dataset?.keySave === 'anthropic'"
                )
                page.keyboard.press("Enter")
                page.locator("[data-runner-status]").get_by_text(saved, exact=True).wait_for()
                assert runner_settings.load(runtime).api_key_set
                assert _tab_until(
                    page, "() => document.activeElement?.name === 'enabled'", steps=150
                )
                page.keyboard.press("Space")
                page.locator("[data-coach-runner] .notice").wait_for()
                assert runner_settings.load(runtime).enabled
                page.evaluate("window.scrollTo(0, 0)")
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert KEY not in page.content()

                # The report page a mail links to: reply by keyboard only.
                page.goto(base + f"/#/coach/report/{entry['id']}")
                reply_id = f"reply-new-{entry['id']}"
                page.locator(f"#{reply_id}").wait_for()
                assert _tab_until(
                    page, f"() => document.activeElement?.id === '{reply_id}'", steps=150
                )
                page.keyboard.type("Akkoord")
                page.keyboard.press("Tab")
                page.keyboard.press("Enter")
                sent = (
                    "Verstuurd. Je coach leest het bij de volgende run."
                    if language == "nl"
                    else "Sent. Your coach reads it at the next run."
                )
                page.get_by_text(sent, exact=True).wait_for()
                with runtime.engine.connect() as conn:
                    texts = conn.execute(select(coach_reply.c.text)).scalars().all()
                assert texts[-1] == "Akkoord" and len(texts) == (1 if language == "en" else 2)
                # The unread reply can still be changed; it saves as you type.
                edit = page.locator("[data-reply-id] textarea").first
                edit.focus()
                page.keyboard.press("End")
                page.keyboard.type(", graag")
                page.keyboard.press("Tab")
                page.locator("[data-reply-edit-status]").first.get_by_text(
                    saved, exact=True
                ).wait_for()
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                # The feed shows the same reply box; an older entry sits in the collapsed
                # archive, which opens by keyboard.
                page.goto(base + "/#/coach")
                archive = page.locator(".coach-archive > summary")
                archive.wait_for()
                archive.focus()
                page.keyboard.press("Enter")
                page.locator(f"#{reply_id}").wait_for()
                violations = _axe(page)
                assert not violations, json.dumps(violations, indent=2)
                assert not errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
