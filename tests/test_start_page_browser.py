"""Start page blocks in Chrome: reorder with the keyboard only, hide and show a block,
reset, choose the best-runs period, workout context on diary notes, and no axe violations
(WCAG 2.x A/AA) in English and Dutch, with the arrange panel open."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.derive import unify
from vibesynergy.model import Activity, DailyMetric, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal import prefs
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import journal
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration


def _seed(rt: Runtime) -> None:
    today = today_in(rt.settings.timezone)
    records: list = [
        Activity(
            "start-run",
            datetime(today.year, today.month, today.day, 7, tzinfo=UTC),
            today,
            "running",
            moving_s=3000,
            distance_m=8000,
            name="Harbour loop",
        ),
        PlannedItem(
            "plan-tomorrow", today + timedelta(days=1), "workout", title="Long run", sport="running"
        ),
        PlannedItem("plan-today", today, "workout", title="Easy swim", sport="swimming"),
        PlannedItem(
            "race-soon",
            today + timedelta(days=4),
            "event",
            title="Dune 15 km",
            is_race=True,
            primary=True,
        ),
    ]
    for age in range(0, 60, 3):
        records.append(DailyMetric(today - timedelta(days=age), "body.weight", 79 + age / 30, "kg"))
    records.append(DailyMetric(today - timedelta(days=1), "body.fat_pct", 14.5, "pct"))
    raw = rt.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="start",
        payload=b"s",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)
    with rt.engine.connect() as conn:
        group = conn.exec_driver_sql(
            "select group_id from activity_unified where name = 'Harbour loop'"
        ).scalar()
    journal.add_note(
        rt.engine,
        text="Strong finish on the hill.",
        start=today,
        activity_id=group,
        source="strava",
        source_ref="s-start",
    )


def _blocks(page) -> list[str]:
    return page.eval_on_selector_all("[data-block]", "els => els.map((el) => el.dataset.block)")


def _wait(predicate, what: str, seconds: float = 15) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def _tab_to(page, test, limit: int = 250) -> None:
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate("() => document.activeElement && document.activeElement.outerHTML"):
            html = page.evaluate("() => document.activeElement.outerHTML.slice(0, 300)")
            if test(html):
                return
    pytest.fail("could not reach the control with Tab")


def test_start_page_blocks_by_keyboard(tmp_path, monkeypatch) -> None:
    from test_portal_a11y import _axe, _axe_script, _chrome, _port, _ready

    chrome = _chrome()
    if chrome is None:
        pytest.skip("Google Chrome is unavailable")
    pytest.importorskip("playwright.sync_api")
    import uvicorn
    from playwright.sync_api import sync_playwright

    port = _port()
    config = tmp_path / "config.toml"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n'
        f"[portal]\nport = {port}\n[sources.demo]\nenabled = true\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    server = thread = None
    try:
        _seed(rt)
        server = uvicorn.Server(
            uvicorn.Config(create_portal_app(rt), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        _ready(base)
        axe = _axe_script()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(axe)
            page = context.new_page()
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            page.locator("[data-block='body'] .dash-body-weight").wait_for(timeout=20000)
            page.locator("[data-bests-range]:not(:empty)").wait_for(timeout=20000)
            page.locator(".dash-upcoming").wait_for(timeout=20000)
            default = _blocks(page)
            assert default[:2] == ["advice", "checkin"]
            assert "Long run" in page.locator("[data-block='upcoming']").inner_text()
            assert "Dune 15 km" in page.locator("[data-block='upcoming']").inner_text()
            # Today's plan shows once, in the advice block, not again under Coming up.
            assert "Easy swim" not in page.locator("[data-block='upcoming']").inner_text()
            assert "Easy swim" in page.locator("[data-block='advice']").inner_text()
            noted = page.locator("[data-block='noted']").inner_text()
            assert "Harbour loop" in noted  # the note says which workout it belongs to
            assert page.locator("[data-block='noted'] .note-activity a").count() == 1

            # Open the arrange panel with the keyboard.
            _tab_to(page, lambda html: "data-arrange-toggle" in html)
            page.keyboard.press("Enter")
            assert page.locator("#dash-arrange").is_visible()
            assert page.get_attribute("[data-arrange-toggle]", "aria-expanded") == "true"
            violations = _axe(page)
            assert not violations, violations

            # Move "Body and measurements" up twice with the keyboard; focus stays on it.
            _tab_to(page, lambda html: 'data-move="-1" data-id="body"' in html)
            before = default.index("body")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page).index("body") == before - 1, "body moved up")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page).index("body") == before - 2, "body moved up twice")
            focused = page.evaluate("() => document.activeElement.dataset.id")
            assert focused == "body"
            status = page.locator("[data-arrange-status]").inner_text()
            assert "Body and measurements" in status
            saved = prefs.load(rt.settings.data_dir)
            assert saved["dashboard_order"].index("body") < saved["dashboard_order"].index(
                default[before - 2]
            )

            # Hide the timeline with Space, then show it again.
            page.focus("input[data-show='timeline']")
            page.keyboard.press("Space")
            _wait(lambda: "timeline" not in _blocks(page), "timeline hidden")
            assert prefs.load(rt.settings.data_dir)["dashboard_hidden"] == ["timeline"]
            page.keyboard.press("Space")
            _wait(lambda: "timeline" in _blocks(page), "timeline shown again")
            _wait(
                lambda: page.evaluate("() => document.activeElement?.dataset?.show") == "timeline",
                "focus back on the timeline checkbox",
            )

            # Reset to the default layout.
            page.focus("[data-arrange-reset]")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page) == default, "default order")
            assert "dashboard_order" not in prefs.load(rt.settings.data_dir)

            # Folded, the advice keeps its one line; unfold it again.
            page.click("[data-collapse='advice']")
            assert page.locator("[data-block='advice'] [data-dash-main]").is_visible()
            assert not page.locator("[data-block='advice'] .dash-plan").is_visible()
            page.click("[data-collapse='advice']")
            # Folded, Coming up keeps its first day.
            page.click("[data-collapse='upcoming']")
            assert page.locator("[data-block='upcoming'] .dash-upcoming > li").first.is_visible()
            page.click("[data-collapse='upcoming']")
            # Folded, Body keeps the weight line with its value.
            page.click("[data-collapse='body']")
            assert page.locator("[data-block='body'] .dash-body-weight .value").is_visible()
            page.click("[data-collapse='body']")
            _wait(
                lambda: not prefs.load(rt.settings.data_dir).get("dashboard_collapsed"),
                "advice unfolded",
            )

            # Fold the timeline with the keyboard: the heading stays, the rest goes,
            # and it is remembered.
            page.focus("[data-collapse='timeline']")
            page.keyboard.press("Enter")
            _wait(
                lambda: prefs.load(rt.settings.data_dir).get("dashboard_collapsed") == ["timeline"],
                "fold saved",
            )
            assert page.get_attribute("[data-collapse='timeline']", "aria-expanded") == "false"
            assert not page.locator("[data-block='timeline'] [data-timeline]").is_visible()

            # Choose the best-runs period; it is saved and shown.
            page.select_option("[data-bests-period]", "all")
            _wait(
                lambda: prefs.load(rt.settings.data_dir).get("best_runs_period") == "all",
                "period saved",
            )
            page.locator("[data-bests-range]:not(:empty)").wait_for()

            # Dutch, on a phone, with the panel open: still no violations.
            page.evaluate("() => localStorage.setItem('vibesynergy-language', 'nl')")
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="domcontentloaded")
            page.locator("[data-block='body'] .dash-body-weight").wait_for(timeout=20000)
            assert "Lichaam en metingen" in page.locator("[data-block='body']").inner_text()
            assert page.eval_on_selector("[data-bests-period]", "el => el.value") == "all"
            assert page.get_attribute("[data-collapse='timeline']", "aria-expanded") == "false"
            page.click("[data-arrange-toggle]")
            violations = _axe(page)
            assert not violations, violations
            width = page.evaluate("() => document.documentElement.scrollWidth")
            assert width <= 390
            browser.close()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=5)
        rt.close()
